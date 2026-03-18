from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import click
import typer

from .application import CodeFactoryService
from .workflow.loader import DEFAULT_WORKFLOW_FILENAME, workflow_file_path
from .workflow.template import initialize_workflow

ACK_FLAG = "--no-guardrails"
_HELP_FLAGS = frozenset({"-h", "--help"})
_CLI_COMMANDS = frozenset({"init", "serve"})

app = typer.Typer(
    add_completion=False,
    help=(
        "Code Factory automation service and project bootstrap CLI. "
        "Use `cf init` to create a starter workflow and `cf serve` to run it."
    ),
    rich_markup_mode="markdown",
)


@dataclass(frozen=True, slots=True)
class CLIConfig:
    """Parsed CLI inputs needed to construct the long-running service."""

    workflow_path: str
    logs_root: str | None
    port: int | None


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


def build_cli_config(
    workflow_path: Path | None,
    logs_root: Path | None,
    port: int | None,
) -> CLIConfig:
    """Resolve CLI path inputs into the normalized runtime configuration."""

    selected_workflow = workflow_path or Path(workflow_file_path())
    resolved_logs_root = (
        None if logs_root is None else str(logs_root.expanduser().resolve())
    )
    return CLIConfig(
        workflow_path=str(selected_workflow.expanduser().resolve()),
        logs_root=resolved_logs_root,
        port=port,
    )


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
    """Create a starter `WORKFLOW.md` in the current working directory."""

    target = Path.cwd() / DEFAULT_WORKFLOW_FILENAME
    try:
        written_path = initialize_workflow(target, force=force)
    except FileExistsError:
        typer.echo(
            f"{written_path_label(target)} already exists. Re-run with `--force` to overwrite it.",
            err=True,
        )
        raise typer.Exit(code=1) from None

    typer.echo(
        f"Created {written_path_label(written_path)} from the bundled default workflow."
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
