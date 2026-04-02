from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console

from .application import CodeFactoryService
from .application.bootstrap import initialize_project
from .errors import ControlRequestError, ReviewError
from .observability.api.client import steer_issue
from .observability.cli_support import (
    CLIConfig,
    build_cli_config,
    resolve_control_endpoint,
)
from .project_init import prepare_project_init
from .trackers.cli import register_tracker_commands
from .workflow.loader import DEFAULT_WORKFLOW_FILENAME
from .workspace.review.review_session import run_review_session

ACK_FLAG = "--no-guardrails"
_HELP_FLAGS = frozenset({"-h", "--help"})
_CLI_COMMANDS = frozenset(
    {"comment", "init", "issue", "review", "serve", "steer", "tracker", "workpad"}
)

app = typer.Typer(
    add_completion=False,
    help=(
        "Code Factory automation service and project bootstrap CLI. "
        "Use `cf init` to create a starter workflow and `cf serve` to run it."
    ),
    rich_markup_mode="markdown",
)
register_tracker_commands(app)


def main(argv: list[str] | None = None) -> int:
    """Run the Typer CLI and normalize exit handling for scripts/tests."""

    args = normalize_cli_args(sys.argv[1:] if argv is None else argv)
    command = typer.main.get_command(app)
    try:
        result = command.main(args=args, prog_name="cf", standalone_mode=False)
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except click.exceptions.Exit as exc:
        return exc.exit_code
    return 0 if result is None else int(result)


def normalize_cli_args(argv: list[str]) -> list[str]:
    """Route bare service invocations through the explicit `serve` subcommand."""

    if not argv:
        return ["serve"]
    if argv[0] in _HELP_FLAGS:
        return argv
    if argv[0] in _CLI_COMMANDS:
        return argv
    return ["serve", *argv]


def run_service(config: CLIConfig) -> int:
    """Start the async service after validating that the workflow file exists."""

    if not Path(config.workflow_path).is_file():
        typer.echo(f"Workflow file not found: {config.workflow_path}", err=True)
        return 1

    try:
        asyncio.run(
            CodeFactoryService(
                config.workflow_path,
                logs_root=config.logs_root,
                port_override=config.port,
            ).run_forever()
        )
    except KeyboardInterrupt:
        return 130
    return 0


@app.command("serve")
def serve_command(
    workflow_path: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "Path to the workflow file to run. Defaults to `./WORKFLOW.md` "
                "when omitted."
            ),
            metavar="WORKFLOW",
        ),
    ] = None,
    logs_root: Annotated[
        Path | None,
        typer.Option(
            "--logs-root",
            help="Enable rotating file logs at `<path>/log/code-factory.log`.",
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            min=0,
            help=(
                "Expose the observability API on this port and override the "
                "workflow setting for the current run."
            ),
        ),
    ] = None,
    no_guardrails: Annotated[
        bool,
        typer.Option(
            ACK_FLAG,
            help=(
                "Required acknowledgement for preview-mode execution of the "
                "coding agent service."
            ),
        ),
    ] = False,
) -> None:
    """Run the long-lived Code Factory service for the selected workflow."""

    if not no_guardrails:
        typer.echo(acknowledgement_banner(), err=True)
        raise typer.Exit(code=1)
    raise typer.Exit(code=run_service(build_cli_config(workflow_path, logs_root, port)))


@app.command("init")
def init_command(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing `WORKFLOW.md` in the current directory.",
        ),
    ] = False,
) -> None:
    """Interactively create a starter workflow and bundled skills in this project."""

    console = Console()
    prepared = prepare_project_init(console=console, target_dir=Path.cwd())
    try:
        result = initialize_project(prepared.values, target_dir=Path.cwd(), force=force)
    except FileExistsError:
        typer.echo(
            "Bootstrap target already exists. Re-run with `--force` to overwrite "
            f"{written_path_label(Path.cwd() / DEFAULT_WORKFLOW_FILENAME)} and "
            f"{written_path_label(Path.cwd() / '.agents' / 'skills')}.",
            err=True,
        )
        raise typer.Exit(code=1) from None

    console.print(
        f"Created {written_path_label(result.workflow_path)} and copied skills to "
        f"{written_path_label(result.skills_path)}."
    )
    for warning in prepared.warnings:
        console.print(f"[yellow]{warning}[/yellow]")
    if prepared.values.tracker_kind != "linear":
        console.print(
            "[yellow]The bundled prompt body still contains Linear-specific guidance. "
            "Review WORKFLOW.md before first use.[/yellow]"
        )


@app.command("review")
def review_command(
    target: Annotated[
        str,
        typer.Argument(help="Ticket identifier or `main` to launch for review."),
    ],
    workflow_path: Annotated[
        Path | None,
        typer.Option(
            "--workflow",
            help="Workflow path used to load review config. Defaults to `./WORKFLOW.md`.",
        ),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option("--keep", help="Keep created review worktrees after exit."),
    ] = False,
) -> None:
    """Create review worktrees and start configured dev servers."""

    resolved_workflow = build_cli_config(workflow_path, None, None).workflow_path
    try:
        asyncio.run(
            run_review_session(resolved_workflow, target, keep=keep, console=Console())
        )
    except ReviewError as exc:
        raise click.ClickException(str(exc)) from exc


@app.command("steer")
def steer_command(
    issue_identifier: Annotated[
        str,
        typer.Argument(
            help="Tracked issue identifier to steer, for example `ENG-123`."
        ),
    ],
    message: Annotated[
        str,
        typer.Argument(help="Steering text to append to the active turn."),
    ],
    workflow_path: Annotated[
        Path | None,
        typer.Option(
            "--workflow",
            help=(
                "Workflow used to discover the running service metadata. "
                "Defaults to `./WORKFLOW.md`."
            ),
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            min=0,
            help="Override the target control-plane port instead of using discovery.",
        ),
    ] = None,
) -> None:
    """Send an operator steering message to a running issue turn."""

    endpoint, resolved_workflow = resolve_control_endpoint(workflow_path, port)
    try:
        result = steer_issue(endpoint, issue_identifier, message)
    except ControlRequestError as exc:
        raise click.ClickException(
            f"{exc.message} (workflow={resolved_workflow}, endpoint={endpoint.base_url})"
        ) from exc
    typer.echo(
        "Steering accepted for "
        f"{result['issue_identifier']} on {result.get('thread_id')}/{result.get('turn_id')} "
        f"via {endpoint.base_url}"
    )


def written_path_label(path: Path) -> str:
    """Return a compact display label for user-facing file paths."""

    return str(path.resolve())


def acknowledgement_banner() -> str:
    """Build the explicit opt-in banner for preview-mode execution."""

    lines = [
        "Code Factory is a low key engineering preview.",
        "The coding agent will run without any guardrails.",
        "Code Factory is not a supported product and is presented as-is.",
        f"To proceed, rerun with `cf serve {ACK_FLAG}`.",
    ]
    width = max(len(line) for line in lines)
    border = "─" * (width + 2)
    content = ["╭" + border + "╮", "│ " + (" " * width) + " │"]
    content.extend(f"│ {line.ljust(width)} │" for line in lines)
    content.extend(["│ " + (" " * width) + " │", "╰" + border + "╯"])
    return "\n".join(content)
