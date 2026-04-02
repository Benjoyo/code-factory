# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportIndexIssue=false, reportInvalidTypeForm=false

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import typer
from rich.console import Console

from code_factory.config.models import ReviewServerSettings
from code_factory.config.review import parse_review_settings
from code_factory.errors import ConfigValidationError, ReviewError, TrackerClientError
from code_factory.runtime.messages import WorkerCleanupComplete
from code_factory.runtime.orchestration.dispatching import DispatchingMixin
from code_factory.runtime.orchestration.failure_policy import (
    RETRY_MODE_WAIT,
    retry_attempt_exhausted,
    transition_issue_to_failure_state,
)
from code_factory.runtime.orchestration.models import RetryEntry
from code_factory.runtime.orchestration.recovery import RecoveryMixin
from code_factory.runtime.orchestration.retrying import RetryingMixin
from code_factory.runtime.worker.workpad import _fallback_workpad_comment
from code_factory.trackers.cli import (
    _body_from_input,
    _cli_allowed_roots,
    _render_human,
    _run_and_render,
    _run_ops,
)
from code_factory.trackers.linear.ops.ops_common import LinearOpsCommon
from code_factory.trackers.linear.ops.ops_files import read_binary_file, resolve_path
from code_factory.trackers.linear.ops.ops_normalize import (
    normalize_issue,
    normalize_project,
    normalize_relation,
    normalize_state,
    normalize_team,
)
from code_factory.trackers.linear.ops.ops_resolution import (
    find_exact,
    find_optional,
    matches_identity,
    require_single,
)
from code_factory.trackers.tooling import UnsupportedTrackerOps, build_tracker_ops
from code_factory.workflow.loader import finalize_prompt_section
from code_factory.workflow.models import WorkflowSnapshot
from code_factory.workflow.profiles.review_profiles import parse_review_types
from code_factory.workspace.review.review_models import (
    ReviewTarget,
    RunningReviewServer,
)
from code_factory.workspace.review.review_output import ReviewConsoleObserver
from code_factory.workspace.review.review_resolution import (
    ensure_github_ready,
    fetch_pull_request,
    resolve_main_ref,
    resolve_repo_root,
    resolve_ticket_target,
)
from code_factory.workspace.review.review_runner import (
    _cancel_log_tasks,
    _create_worktree,
    _head_sha,
    _open_review_urls,
    _review_temp_root,
    _wait_for_exit,
)
from code_factory.workspace.review.review_shell import ShellResult
from code_factory.workspace.review.review_templates import (
    build_review_environment,
    computed_review_port,
    render_review_template,
)
from code_factory.workspace.workpad import WORKPAD_FILENAME

from .conftest import make_issue, make_snapshot, write_workflow_file


def test_review_settings_edge_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_TMP", str(tmp_path / "review-root"))
    settings = parse_review_settings({"review": {"temp_root": "$REVIEW_TMP"}})
    assert settings.temp_root == str((tmp_path / "review-root").resolve())
    assert settings.prepare is None
    assert settings.servers == ()

    monkeypatch.setenv("EMPTY_TMP", "")
    assert (
        parse_review_settings({"review": {"temp_root": "$EMPTY_TMP"}}).temp_root is None
    )

    with pytest.raises(ConfigValidationError, match="review.servers must be a list"):
        parse_review_settings({"review": {"servers": "bad"}})
    with pytest.raises(ConfigValidationError, match="review.servers must not be empty"):
        parse_review_settings({"review": {"servers": []}})
    with pytest.raises(
        ConfigValidationError, match="review.servers names must be unique"
    ):
        parse_review_settings(
            {
                "review": {
                    "servers": [
                        {"name": "web", "command": "run"},
                        {"name": "web", "command": "run 2"},
                    ]
                }
            }
        )
    with pytest.raises(
        ConfigValidationError,
        match="review.servers\\[0\\].base_port must be greater than 0",
    ):
        parse_review_settings(
            {"review": {"servers": [{"name": "web", "command": "run", "base_port": 0}]}}
        )
    with pytest.raises(
        ConfigValidationError,
        match="review.servers\\[0\\].open_browser must be a boolean",
    ):
        parse_review_settings(
            {
                "review": {
                    "servers": [
                        {"name": "web", "command": "run", "open_browser": "yes"}
                    ]
                }
            }
        )


def test_workflow_review_helpers_cover_remaining_paths(tmp_path: Path) -> None:
    sections: dict[str, str] = {}
    assert finalize_prompt_section(sections, "default", ["Body"]) is None
    assert sections == {"default": "Body"}

    with pytest.raises(ConfigValidationError, match="ai_review has unsupported keys"):
        parse_review_types({"ai_review": {"unknown": True}}, {"security": "body"})
    with pytest.raises(
        ConfigValidationError, match="ai_review.types must be an object"
    ):
        parse_review_types({"ai_review": {"types": []}}, {"security": "body"})
    with pytest.raises(
        ConfigValidationError, match="ai_review.types keys must not be blank"
    ):
        parse_review_types(
            {"ai_review": {"types": {" ": {"prompt": "security"}}}},
            {"security": "body"},
        )
    with pytest.raises(ConfigValidationError, match="duplicate normalized review"):
        parse_review_types(
            {
                "ai_review": {
                    "types": {
                        "Security": {"prompt": "security"},
                        " security ": {"prompt": "security"},
                    }
                }
            },
            {"security": "body"},
        )
    with pytest.raises(ConfigValidationError, match="unsupported keys"):
        parse_review_types(
            {"ai_review": {"types": {"Security": {"prompt": "security", "bad": True}}}},
            {"security": "body"},
        )
    with pytest.raises(
        ConfigValidationError, match="requires named `# review:` sections"
    ):
        parse_review_types(
            {"ai_review": {"types": {"Security": {"prompt": "security"}}}}, {}
        )
    with pytest.raises(
        ConfigValidationError, match="paths.only must be a list of strings"
    ):
        parse_review_types(
            {
                "ai_review": {
                    "types": {
                        "Security": {"prompt": "security", "paths": {"only": "src/**"}}
                    }
                }
            },
            {"security": "body"},
        )
    with pytest.raises(
        ConfigValidationError, match="paths.include entries must not be blank"
    ):
        parse_review_types(
            {
                "ai_review": {
                    "types": {
                        "Security": {"prompt": "security", "paths": {"include": [" "]}}
                    }
                }
            },
            {"security": "body"},
        )
    with pytest.raises(
        ConfigValidationError, match="paths.exclude must not contain duplicates"
    ):
        parse_review_types(
            {
                "ai_review": {
                    "types": {
                        "Security": {
                            "prompt": "security",
                            "paths": {"exclude": ["tests/**", "tests/**"]},
                        }
                    }
                }
            },
            {"security": "body"},
        )
    with pytest.raises(
        ConfigValidationError,
        match="ai_review.types.Security.prompt must be a string or list of strings",
    ):
        parse_review_types(
            {"ai_review": {"types": {"Security": {"prompt": 1}}}},
            {"security": "body"},
        )

    snapshot = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    assert snapshot.ai_review_types_for_state("In Progress") == ()
    assert snapshot.ai_review_type("missing") is None


def test_review_template_and_emit_output_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    main_target = ReviewTarget(
        target="main",
        kind="main",
        ticket_identifier=None,
        ticket_number=None,
        ref="origin/main",
    )
    env = build_review_environment(main_target, worktree="/tmp/review", port=None)
    assert "CF_REVIEW_TICKET_NUMBER" not in env
    assert "CF_REVIEW_PORT" not in env
    assert computed_review_port(None, main_target) is None
    assert computed_review_port(3000, main_target) == 3000
    with pytest.raises(ReviewError, match="outside the valid range"):
        computed_review_port(70_000, main_target)
    with pytest.raises(ReviewError, match="Failed to render review review template"):
        render_review_template("{{ review.missing.value }}", {"review": {}})

    console_io = io.StringIO()
    console = Console(file=console_io, force_terminal=False, color_system=None)
    observer = ReviewConsoleObserver(console)
    observer.on_prepare_line("label", "stdout", "out")
    observer.on_prepare_line("label", "stderr", "err")
    rendered = console_io.getvalue()
    assert "[label:stdout] out" in rendered
    assert "[label:stderr] err" in rendered


@pytest.mark.asyncio
async def test_review_resolution_edge_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok_capture(
        command: str, *, cwd: str, env: dict[str, str] | None = None
    ) -> ShellResult:
        if command == "git symbolic-ref --quiet --short refs/remotes/origin/HEAD":
            return ShellResult(1, "", "")
        if command == "git fetch origin":
            return ShellResult(0, "", "")
        if command.startswith("gh auth status"):
            return ShellResult(1, "", "not logged in")
        if command.startswith("git rev-parse --show-toplevel"):
            return ShellResult(1, "", "")
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.review.review_resolution.shutil.which",
        lambda _name: None,
    )
    with pytest.raises(ReviewError, match="GitHub CLI"):
        await ensure_github_ready("/repo", shell_capture=ok_capture)

    monkeypatch.setattr(
        "code_factory.workspace.review.review_resolution.shutil.which",
        lambda _name: "/usr/bin/gh",
    )
    with pytest.raises(ReviewError, match="not logged in"):
        await ensure_github_ready("/repo", shell_capture=ok_capture)

    assert await resolve_main_ref("/repo", shell_capture=ok_capture) == "origin/main"
    with pytest.raises(
        ReviewError, match="Workflow root is not inside a git repository"
    ):
        await resolve_repo_root(str(tmp_path / "WORKFLOW.md"), shell_capture=ok_capture)

    class TrackerMissing:
        async def fetch_issue_by_identifier(self, _identifier: str) -> None:
            return None

    with pytest.raises(ReviewError, match="Ticket not found"):
        await resolve_ticket_target(
            TrackerMissing(), "/repo", "ENG-1", shell_capture=ok_capture
        )

    class TrackerNoBranch:
        async def fetch_issue_by_identifier(self, _identifier: str):
            return make_issue(identifier="ENG-1", branch_name=None)

    with pytest.raises(ReviewError, match="does not have tracker branch metadata"):
        await resolve_ticket_target(
            TrackerNoBranch(), "/repo", "ENG-1", shell_capture=ok_capture
        )

    async def payload_capture(
        command: str, *, cwd: str, env: dict[str, str] | None = None
    ) -> ShellResult:
        if "invalid-json" in command:
            return ShellResult(0, "[", "")
        if "wrong-type" in command:
            return ShellResult(0, '{"bad":true}', "")
        if "missing-number" in command:
            return ShellResult(0, '[{"url":"u","headRefOid":"sha"}]', "")
        if "missing-url" in command:
            return ShellResult(0, '[{"number":1,"headRefOid":"sha"}]', "")
        if "missing-head" in command:
            return ShellResult(0, '[{"number":1,"url":"u"}]', "")
        if "two-prs" in command:
            return ShellResult(
                0,
                '[{"number":1,"url":"u","headRefOid":"a"},{"number":2,"url":"v","headRefOid":"b"}]',
                "",
            )
        if "no-prs" in command:
            return ShellResult(0, "[]", "")
        if "bad-object" in command:
            return ShellResult(0, '["bad"]', "")
        raise AssertionError(command)

    for branch_name, message in [
        ("invalid-json", "invalid PR JSON"),
        ("wrong-type", "invalid PR list payload"),
        ("no-prs", "No open PR found"),
        ("two-prs", "Multiple open PRs found"),
        ("bad-object", "invalid PR object"),
        ("missing-number", "missing `number`"),
        ("missing-url", "missing `url`"),
        ("missing-head", "missing `headRefOid`"),
    ]:
        with pytest.raises(ReviewError, match=message):
            await fetch_pull_request(
                "/repo", branch_name, shell_capture=payload_capture
            )


@pytest.mark.asyncio
async def test_review_runner_edge_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    with pytest.raises(ReviewError, match="review.servers"):
        from code_factory.workspace.review.review_session import run_review_session

        await run_review_session(str(workflow), "main", keep=False)

    assert _review_temp_root(None, "/repo/project") == str(
        Path(cast(str, _review_temp_root(None, "/repo/project")))
    )

    existing = tmp_path / "existing"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("", encoding="utf-8")
    with pytest.raises(ReviewError, match="already exists"):
        await _create_worktree(
            "/repo",
            str(existing),
            ReviewTarget("ENG-1", "ticket", "ENG-1", 1, "abc"),
        )

    async def failed_capture(
        command: str, *, cwd: str, env: dict[str, str] | None = None
    ) -> ShellResult:
        if command.startswith("git worktree add --detach"):
            return ShellResult(1, "", "boom")
        if command == "git rev-parse --short HEAD":
            return ShellResult(1, "", "")
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.review.review_runner.capture_shell", failed_capture
    )
    with pytest.raises(ReviewError, match="Failed to create review worktree"):
        await _create_worktree(
            "/repo",
            str(tmp_path / "missing"),
            ReviewTarget("ENG-2", "ticket", "ENG-2", 2, "abc"),
        )
    with pytest.raises(ReviewError, match="Failed to resolve HEAD"):
        await _head_sha("/repo")

    class FakeProcess:
        def __init__(self, status: int) -> None:
            self._status = status
            self.pid = 1

        async def wait(self) -> int:
            return self._status

        async def terminate(self) -> None:
            return None

    failing_entry = RunningReviewServer(
        target=ReviewTarget("ENG-3", "ticket", "ENG-3", 3, "sha"),
        launch=SimpleNamespace(name="web", url=None, open_browser=False),
        worktree="/tmp/worktree",
        process=SimpleNamespace(
            wait=FakeProcess(2).wait, terminate=FakeProcess(2).terminate, pid=1
        ),
        head_sha="sha",
    )
    with pytest.raises(ReviewError, match="exited with status 2"):
        await _wait_for_exit([cast(Any, failing_entry)], stop_event=None)

    console_io = io.StringIO()
    console = Console(file=console_io, force_terminal=False, color_system=None)
    browser_entry = RunningReviewServer(
        target=ReviewTarget("ENG-4", "ticket", "ENG-4", 4, "sha"),
        launch=SimpleNamespace(
            name="web", url="http://127.0.0.1:3004", open_browser=True
        ),
        worktree="/tmp/worktree",
        process=SimpleNamespace(pid=1),
        head_sha="sha",
    )
    monkeypatch.setattr(
        "code_factory.workspace.review.review_runner.wait_for_http_ready",
        lambda _url: asyncio.sleep(0, result=False),
    )
    await _open_review_urls(ReviewConsoleObserver(console), [cast(Any, browser_entry)])
    assert "not opened automatically" in console_io.getvalue()

    console_io = io.StringIO()
    console = Console(file=console_io, force_terminal=False, color_system=None)
    monkeypatch.setattr(
        "code_factory.workspace.review.review_runner.wait_for_http_ready",
        lambda _url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review.review_runner._open_browser", lambda _url: False
    )
    await _open_review_urls(ReviewConsoleObserver(console), [cast(Any, browser_entry)])
    assert "Failed to open browser" in console_io.getvalue()

    task = asyncio.create_task(asyncio.sleep(10))
    await _cancel_log_tasks([task])
    assert task.cancelled() is True


def test_tracker_cli_and_tooling_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsupported = cast(Any, UnsupportedTrackerOps("memory"))
    with pytest.raises(TrackerClientError, match="Linear-backed workflow"):
        asyncio.run(unsupported.read_issue("ENG-1"))
    assert asyncio.run(unsupported.close()) is None

    settings = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md")).settings
    monkeypatch.setattr(
        "code_factory.trackers.linear.LinearOps.from_settings",
        lambda settings, *, allowed_roots=(): ("linear", settings, allowed_roots),
    )
    assert build_tracker_ops(settings, allowed_roots=("/tmp",))[0] == "linear"

    body_file = tmp_path / "body.md"
    body_file.write_text("body\n", encoding="utf-8")
    assert _body_from_input("inline", None) == "inline"
    assert _body_from_input(None, str(body_file)) == "body\n"
    assert _body_from_input(None, "ignored", allow_file=False) is None

    monkeypatch.setattr(
        "code_factory.trackers.cli.sys.stdin",
        SimpleNamespace(isatty=lambda: False, read=lambda: "stdin-body"),
    )
    assert _body_from_input(None, None) == "stdin-body"
    monkeypatch.setattr(
        "code_factory.trackers.cli.sys.stdin", SimpleNamespace(isatty=lambda: True)
    )
    with pytest.raises(
        typer.BadParameter,
        match="one of `--body`, `--file`, or stdin input is required",
    ):
        _body_from_input(None, None)

    rendered: list[dict[str, Any]] = []

    async def fake_run_ops_issue(workflow: Any, callback: Any) -> dict[str, Any]:
        return {
            "issue": {"identifier": "ENG-1", "title": "Fix", "state": {"name": "Todo"}}
        }

    monkeypatch.setattr("code_factory.trackers.cli._run_ops", fake_run_ops_issue)
    monkeypatch.setattr(
        "code_factory.trackers.cli.typer.echo",
        lambda text: rendered.append({"echo": text}),
    )
    _run_and_render(None, False, lambda _ops: None)  # type: ignore[arg-type]
    assert rendered[-1]["echo"] == "ENG-1: Fix [Todo]"

    rendered_json: list[str] = []

    async def fake_run_ops_json(workflow: Any, callback: Any) -> dict[str, Any]:
        return {"x": 1}

    monkeypatch.setattr("code_factory.trackers.cli._run_ops", fake_run_ops_json)
    monkeypatch.setattr(
        "code_factory.trackers.cli.typer.echo", lambda text: rendered_json.append(text)
    )
    _run_and_render(None, True, lambda _ops: None)  # type: ignore[arg-type]
    assert '"x": 1' in rendered_json[-1]

    class FakeOps:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    fake_ops = FakeOps()
    monkeypatch.setattr(
        "code_factory.trackers.cli.build_tracker_ops",
        lambda settings, *, allowed_roots: fake_ops,
    )
    monkeypatch.chdir(tmp_path)
    write_workflow_file(tmp_path / "WORKFLOW.md")

    async def callback(_ops: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_run_ops(None, callback))
    assert fake_ops.closed is True

    roots = _cli_allowed_roots(settings)
    assert roots[0] == str(Path.cwd().resolve())
    assert len(set(roots)) == len(roots)

    console_calls: list[str] = []
    monkeypatch.setattr(
        "code_factory.trackers.cli.console.print_json",
        lambda *, data: console_calls.append(data),
    )
    _render_human({"projects": [{"name": "Project"}]})
    _render_human({"project": {"name": "Project"}})
    _render_human({"other": True})
    assert console_calls == ['{"other": true}']


def test_linear_file_resolution_and_normalization_edges(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    inside = tmp_path / "inside.bin"
    inside.write_bytes(b"\x01")

    with pytest.raises(TrackerClientError, match="outside the allowed workspace roots"):
        resolve_path(str(outside), (str(tmp_path / "root"),))

    filename, content, content_type = read_binary_file(str(inside), ())
    assert filename == "inside.bin"
    assert content == b"\x01"
    assert content_type == "application/octet-stream"

    assert normalize_state(None) is None
    assert normalize_team(None, include_states=True) is None
    assert normalize_project(None, include_teams=True) is None
    assert matches_identity({"id": " ENG-1 "}, "eng-1", "id") is True
    assert find_optional([{"id": "ENG-1"}], "missing", "id") is None
    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        find_exact([], "missing", "id")
    with pytest.raises(TrackerClientError, match="`team` is required"):
        require_single([], "Project", field_name="team")
    with pytest.raises(TrackerClientError, match="multiple teams"):
        require_single([{"id": "1"}, {"id": "2"}], "Project", field_name="team")

    relation = normalize_relation({"type": "blocks"}, "issue")
    assert relation["issue"]["id"] is None
    issue = normalize_issue(
        {
            "id": "issue-1",
            "identifier": "ENG-1",
            "title": "Fix",
            "priority": 1,
            "url": "https://example/ENG-1",
            "branchName": None,
            "state": {"id": "state-1", "name": "Todo", "type": "unstarted"},
            "project": {
                "id": "project-1",
                "name": "Project",
                "slugId": "project",
                "url": "https://example/project",
            },
            "team": {"id": "team-1", "name": "Team", "key": "ENG"},
            "labels": {"nodes": [{"name": "Bug"}]},
        },
        include_description=False,
        include_comments=False,
        include_attachments=False,
        include_relations=False,
    )
    assert "description" not in issue


@pytest.mark.asyncio
async def test_linear_common_and_runtime_helper_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md")).settings

    closed: list[str] = []

    class FakeClient:
        def __init__(self, _settings: Any) -> None:
            self.request = lambda query, variables: asyncio.sleep(
                0, result={"data": {"ok": True}}
            )

        async def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(
        "code_factory.trackers.linear.ops.ops_common.LinearGraphQLClient", FakeClient
    )
    ops = LinearOpsCommon.from_settings(settings)
    assert await ops.raw_graphql("query") == {"data": {"ok": True}}
    await ops.close()
    assert closed == ["closed"]

    noop_ops = LinearOpsCommon(
        settings, lambda query, variables: asyncio.sleep(0, result={})
    )
    await noop_ops.close()
    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        await noop_ops._issue_node(
            "ENG-1",
            include_description=False,
            include_comments=False,
            include_attachments=False,
            include_relations=False,
        )
    assert await noop_ops._projects() == []
    assert await noop_ops._teams() == []
    assert noop_ops._matches_issue({}, project="x", state=None, query=None) is False
    assert (
        noop_ops._matches_issue(
            {"project": {"id": "x"}, "state": {"name": "Todo"}},
            project="x",
            state="todo",
            query="",
        )
        is True
    )
    assert (
        noop_ops._matches_issue(
            {"identifier": "ENG-1", "title": "Fix", "description": ""},
            project=None,
            state=None,
            query="eng-1",
        )
        is True
    )
    assert noop_ops._error_message([{}]) == "unknown tracker error"

    assert (
        retry_attempt_exhausted(
            make_snapshot(write_workflow_file(tmp_path / "WORKFLOW-2.md")),
            mode=RETRY_MODE_WAIT,
            attempt=99,
        )
        is False
    )

    tracker_calls: list[tuple[str, Any]] = []

    class FailureTracker:
        async def update_issue_state(self, issue_id: str, state: str) -> None:
            tracker_calls.append(("update", (issue_id, state)))

    with pytest.raises(RuntimeError, match="missing_issue_id_for_failure_transition"):
        await transition_issue_to_failure_state(
            make_snapshot(write_workflow_file(tmp_path / "WORKFLOW-3.md")),
            cast(Any, FailureTracker()),
            make_issue(id=None, state=None),
            summary="blocked",
        )

    assert (
        await _fallback_workpad_comment(cast(Any, object()), make_issue(id=None))
        is None
    )


class _FakeDispatching(DispatchingMixin):
    pass


class _FakeRetrying(RetryingMixin):
    FAILURE_RETRY_BASE_MS = 100


class _FakeRecovery(RecoveryMixin):
    pass


@pytest.mark.asyncio
async def test_runtime_mixin_edge_paths(tmp_path: Path) -> None:
    workflow = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    issue = make_issue()

    dispatch = _FakeDispatching()
    dispatch.settings = workflow.settings
    dispatch.claimed = set()
    dispatch.running = {}
    dispatch.tracker = SimpleNamespace(
        fetch_issue_states_by_ids=lambda ids: asyncio.sleep(0, result=[]),
    )
    assert dispatch._should_dispatch_issue(make_issue(id=None)) is False
    assert await dispatch._revalidate_issue_for_dispatch(issue) is None

    retrying = _FakeRetrying()
    retrying.settings = workflow.settings
    retrying.workflow_snapshot = workflow
    retrying.retry_entries = {}
    retrying.claimed = set()
    retrying.running = {}
    retrying.tracker = SimpleNamespace(
        fetch_issue_states_by_ids=lambda ids: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
    )
    retrying._workspace_manager_for_path = lambda path: SimpleNamespace(
        remove=lambda path: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert await retrying._refresh_retry_issue("issue-1") is None
    await retrying._cleanup_retry_issue_workspace(issue, "/tmp/workspace")
    retrying.retry_entries["issue-1"] = RetryEntry(
        issue_id="issue-1",
        identifier="ENG-1",
        attempt=2,
        due_at_ms=1,
        token="token",
        error="old",
        workspace_path="/tmp/workspace",
        state_name="In Progress",
        mode="failure",
    )
    retrying._schedule_issue_retry("issue-1", None, identifier=None, mode="")
    assert retrying.retry_entries["issue-1"].attempt == 3
    assert retrying.retry_entries["issue-1"].mode == "failure"

    recovery = _FakeRecovery()
    cleanup_messages: list[WorkerCleanupComplete] = []
    recovery.queue = SimpleNamespace(
        put=lambda message: asyncio.sleep(0, result=cleanup_messages.append(message))
    )
    recovery._workspace_manager_for_path = lambda path: SimpleNamespace(
        remove=lambda path: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    await recovery._cleanup_workspace_after_exit("issue-1", "/tmp/workspace")
    assert cleanup_messages[0].error is not None
