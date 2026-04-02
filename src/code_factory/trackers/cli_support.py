from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import click
import typer
from rich.console import Console

from ..config import parse_settings
from ..errors import TrackerClientError
from ..workflow import load_workflow
from .tooling import TrackerOps, build_tracker_ops
from .user_errors import tracker_error_payload

console = Console()


def _run_and_render(
    workflow: Path | None,
    as_json: bool,
    callback: Callable[[TrackerOps], Awaitable[dict[str, Any]]],
) -> None:
    try:
        payload = asyncio.run(_run_ops(workflow, callback))
    except TrackerClientError as exc:
        error = tracker_error_payload(exc).get("error") or {}
        raise click.ClickException(str(error.get("message") or exc)) from exc
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _render_human(payload)


async def _run_ops(
    workflow: Path | None,
    callback: Callable[[TrackerOps], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    resolved = workflow or Path.cwd() / "WORKFLOW.md"
    settings = parse_settings(load_workflow(str(resolved)).config)
    ops = build_tracker_ops(settings, allowed_roots=_cli_allowed_roots(settings))
    try:
        return await callback(ops)
    finally:
        await ops.close()


def _cli_allowed_roots(settings) -> tuple[str, ...]:
    roots = [str(Path.cwd().resolve())]
    if settings.workspace.root not in roots:
        roots.append(settings.workspace.root)
    return tuple(roots)


def _render_human(payload: dict[str, Any]) -> None:
    if "issue" in payload:
        issue = payload["issue"]
        typer.echo(
            f"{issue.get('identifier')}: {issue.get('title')} [{(issue.get('state') or {}).get('name')}]"
        )
        return
    if "issues" in payload:
        for issue in payload["issues"]:
            typer.echo(
                f"{issue.get('identifier')}: {issue.get('title')} [{(issue.get('state') or {}).get('name')}]"
            )
        return
    if "projects" in payload:
        for project in payload["projects"]:
            typer.echo(f"{project.get('name')}")
        return
    if "project" in payload:
        project = payload["project"]
        typer.echo(f"{project.get('name')}")
        return
    console.print_json(data=json.dumps(payload))
