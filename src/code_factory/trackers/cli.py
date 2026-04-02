from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from ..config import parse_settings
from ..errors import TrackerClientError
from ..workflow import load_workflow
from .cli_support import _cli_allowed_roots, _render_human, console
from .tooling import TrackerOps, build_tracker_ops
from .user_errors import tracker_error_payload


def register_tracker_commands(app: typer.Typer) -> None:
    issue_app = typer.Typer(help="Inspect and mutate tracker issues.")
    comment_app = typer.Typer(help="Inspect and mutate tracker comments.")
    workpad_app = typer.Typer(help="Inspect and sync the persistent workpad comment.")
    tracker_app = typer.Typer(help="Tracker admin helpers.")
    app.add_typer(issue_app, name="issue")
    app.add_typer(comment_app, name="comment")
    app.add_typer(workpad_app, name="workpad")
    app.add_typer(tracker_app, name="tracker")

    @issue_app.command("get")
    def issue_get(
        issue: str,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        _run_and_render(
            workflow,
            as_json,
            lambda ops: ops.read_issue(
                issue,
                include_description=True,
                include_comments=True,
                include_attachments=True,
                include_relations=True,
            ),
        )

    @issue_app.command("list")
    def issue_list(
        project: Annotated[str | None, typer.Option("--project")] = None,
        state: Annotated[str | None, typer.Option("--state")] = None,
        query: Annotated[str | None, typer.Option("--query")] = None,
        limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        _run_and_render(
            workflow,
            as_json,
            lambda ops: ops.read_issues(
                project=project,
                state=state,
                query=query,
                limit=limit,
                include_description=False,
                include_comments=False,
                include_attachments=False,
                include_relations=False,
            ),
        )

    @issue_app.command("create")
    def issue_create(
        title: Annotated[str, typer.Argument()],
        description: Annotated[str | None, typer.Option("--description")] = None,
        project: Annotated[str | None, typer.Option("--project")] = None,
        team: Annotated[str | None, typer.Option("--team")] = None,
        state: Annotated[str | None, typer.Option("--state")] = None,
        priority: Annotated[
            int | None, typer.Option("--priority", min=0, max=4)
        ] = None,
        assignee: Annotated[str | None, typer.Option("--assignee")] = None,
        labels: Annotated[list[str] | None, typer.Option("--label")] = None,
        blocked_by: Annotated[list[str] | None, typer.Option("--blocked-by")] = None,
        blocks: Annotated[list[str] | None, typer.Option("--blocks")] = None,
        related_to: Annotated[list[str] | None, typer.Option("--related-to")] = None,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        _run_and_render(
            workflow,
            as_json,
            lambda ops: ops.create_issue(
                title=title,
                description=description,
                project=project,
                team=team,
                state=state,
                priority=priority,
                assignee=assignee,
                labels=labels or [],
                blocked_by=blocked_by or [],
                blocks=blocks or [],
                related_to=related_to or [],
            ),
        )

    @issue_app.command("update")
    def issue_update(
        issue: str,
        title: Annotated[str | None, typer.Option("--title")] = None,
        description: Annotated[str | None, typer.Option("--description")] = None,
        project: Annotated[str | None, typer.Option("--project")] = None,
        team: Annotated[str | None, typer.Option("--team")] = None,
        state: Annotated[str | None, typer.Option("--state")] = None,
        priority: Annotated[
            int | None, typer.Option("--priority", min=0, max=4)
        ] = None,
        assignee: Annotated[str | None, typer.Option("--assignee")] = None,
        labels: Annotated[list[str] | None, typer.Option("--label")] = None,
        blocked_by: Annotated[list[str] | None, typer.Option("--blocked-by")] = None,
        blocks: Annotated[list[str] | None, typer.Option("--blocks")] = None,
        related_to: Annotated[list[str] | None, typer.Option("--related-to")] = None,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        _run_and_render(
            workflow,
            as_json,
            lambda ops: ops.update_issue(
                issue,
                title=title,
                description=description,
                project=project,
                team=team,
                state=state,
                priority=priority,
                assignee=assignee,
                labels=labels or [],
                blocked_by=blocked_by or [],
                blocks=blocks or [],
                related_to=related_to or [],
            ),
        )

    @issue_app.command("move")
    def issue_move(
        issue: str,
        state: str,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        _run_and_render(workflow, as_json, lambda ops: ops.move_issue(issue, state))

    @issue_app.command("link-pr")
    def issue_link_pr(
        issue: str,
        url: str,
        title: Annotated[str | None, typer.Option("--title")] = None,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        _run_and_render(workflow, as_json, lambda ops: ops.link_pr(issue, url, title))

    @comment_app.command("list")
    def comment_list(
        issue: str,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        _run_and_render(workflow, as_json, lambda ops: ops.list_comments(issue))

    @comment_app.command("create")
    def comment_create(
        issue: str,
        body: Annotated[str | None, typer.Option("--body")] = None,
        file_path: Annotated[str | None, typer.Option("--file")] = None,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        comment_body = _body_from_input(body, file_path)
        assert comment_body is not None
        _run_and_render(
            workflow, as_json, lambda ops: ops.create_comment(issue, comment_body)
        )

    @comment_app.command("update")
    def comment_update(
        comment_id: str,
        body: Annotated[str | None, typer.Option("--body")] = None,
        file_path: Annotated[str | None, typer.Option("--file")] = None,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        comment_body = _body_from_input(body, file_path)
        assert comment_body is not None
        _run_and_render(
            workflow, as_json, lambda ops: ops.update_comment(comment_id, comment_body)
        )

    @workpad_app.command("get")
    def workpad_get(
        issue: str,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        _run_and_render(workflow, as_json, lambda ops: ops.get_workpad(issue))

    @workpad_app.command("sync")
    def workpad_sync(
        issue: str,
        body: Annotated[str | None, typer.Option("--body")] = None,
        file_path: Annotated[str | None, typer.Option("--file")] = None,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        inline_body = _body_from_input(body, file_path, allow_file=False)
        _run_and_render(
            workflow,
            as_json,
            lambda ops: ops.sync_workpad(issue, body=inline_body, file_path=file_path),
        )

    @tracker_app.command("raw", hidden=True)
    def tracker_raw(
        query: Annotated[str, typer.Option("--query")],
        variables: Annotated[str | None, typer.Option("--variables")] = None,
        workflow: Annotated[Path | None, typer.Option("--workflow")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        parsed = json.loads(variables) if variables else {}
        _run_and_render(workflow, as_json, lambda ops: ops.raw_graphql(query, parsed))


def _body_from_input(
    body: str | None, file_path: str | None, *, allow_file: bool = True
) -> str | None:
    if body is not None:
        return body
    if file_path is not None:
        return None if not allow_file else Path(file_path).read_text(encoding="utf-8")
    if body is None and file_path is None and not sys.stdin.isatty():
        return sys.stdin.read()
    raise typer.BadParameter("one of `--body`, `--file`, or stdin input is required")


def _run_and_render(
    workflow: Path | None,
    as_json: bool,
    callback,
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
    callback,
) -> dict[str, Any]:
    resolved = workflow or Path.cwd() / "WORKFLOW.md"
    settings = parse_settings(load_workflow(str(resolved)).config)
    ops: TrackerOps = build_tracker_ops(
        settings, allowed_roots=_cli_allowed_roots(settings)
    )
    try:
        return await callback(ops)
    finally:
        await ops.close()
