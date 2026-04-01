# pyright: reportAttributeAccessIssue=false, reportInvalidTypeForm=false

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rich.console import Console

from code_factory.config.review import parse_review_settings
from code_factory.errors import ConfigValidationError, ReviewError
from code_factory.issues import IssueComment
from code_factory.runtime.orchestration.dispatching import DispatchingMixin
from code_factory.runtime.orchestration.models import RetryEntry
from code_factory.runtime.orchestration.recovery import RecoveryMixin
from code_factory.runtime.orchestration.retrying import RetryingMixin
from code_factory.runtime.worker.quality_gates.readiness import native_readiness_result
from code_factory.runtime.worker.workpad import (
    _load_workpad_body,
    sync_workspace_workpad,
)
from code_factory.trackers.cli import _cli_allowed_roots, _render_human
from code_factory.trackers.tooling import UnsupportedTrackerOps, build_tracker_ops
from code_factory.workflow.profiles.review_profiles import (
    WorkflowReviewType,
    parse_review_types,
    parse_state_review_refs,
)
from code_factory.workspace.repository import (
    _upstream_display_name,
    upstream_head_sha,
    upstream_name,
)
from code_factory.workspace.review.review_models import ReviewTarget
from code_factory.workspace.review.review_output import ReviewConsoleObserver
from code_factory.workspace.review.review_resolution import (
    fetch_pull_request,
    resolve_repo_root,
    resolve_review_target,
    trailing_ticket_number,
)
from code_factory.workspace.review.review_runner import (
    ReviewRunner,
    _cancel_log_tasks,
    _create_worktree,
    _head_sha,
    _log_tasks,
    _stream_output,
    _wait_for_exit,
)
from code_factory.workspace.review.review_session import run_review_session
from code_factory.workspace.review.review_shell import ShellResult

from .conftest import make_issue, make_snapshot, write_workflow_file


class _DispatchCoverage(DispatchingMixin):
    pass


class _RetryCoverage(RetryingMixin):
    FAILURE_RETRY_BASE_MS = 100


class _RecoveryCoverage(RecoveryMixin):
    pass


def _review_console() -> tuple[Console, io.StringIO]:
    output = io.StringIO()
    return Console(file=output, force_terminal=False, color_system=None), output


def test_remaining_config_workflow_and_tracker_cli_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        ConfigValidationError, match="review.servers\\[0\\].name can't be blank"
    ):
        parse_review_settings(
            {"review": {"servers": [{"name": " ", "command": "run"}]}}
        )

    settings = parse_review_settings(
        {
            "review": {
                "servers": [{"name": "web", "command": "run", "open_browser": False}]
            }
        }
    )
    assert settings.servers[0].open_browser is False

    assert parse_review_types({"ai_review": {}}, {"security": "body"}) == {}
    review_types = {
        "security": WorkflowReviewType(review_name="Security", prompt_ref="security")
    }
    with pytest.raises(ConfigValidationError, match="must not be empty"):
        parse_state_review_refs([], "states.In Progress", review_types)
    with pytest.raises(
        ConfigValidationError, match="must be a string or list of strings"
    ):
        parse_state_review_refs(1, "states.In Progress", review_types)
    with pytest.raises(
        ConfigValidationError, match="must not contain duplicate normalized reviews"
    ):
        parse_state_review_refs(
            ["Security", " security "], "states.In Progress", review_types
        )
    with pytest.raises(ConfigValidationError, match="entries must not be blank"):
        parse_state_review_refs([" "], "states.In Progress", review_types)
    with pytest.raises(ConfigValidationError, match="entries must not be blank"):
        parse_review_types(
            {"ai_review": {"types": {"Security": {"prompt": " "}}}},
            {"security": "body"},
        )
    with pytest.raises(ConfigValidationError, match="must be a list of strings"):
        parse_review_types(
            {
                "ai_review": {
                    "types": {
                        "Security": {"prompt": "security", "paths": {"only": [1]}}
                    }
                }
            },
            {"security": "body"},
        )

    unsupported = build_tracker_ops(
        cast(Any, SimpleNamespace(tracker=SimpleNamespace(kind="memory")))
    )
    assert isinstance(unsupported, UnsupportedTrackerOps)

    monkeypatch.chdir(tmp_path)
    settings = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            workspace={"root": str(tmp_path.resolve())},
        )
    ).settings
    assert _cli_allowed_roots(settings) == (str(tmp_path.resolve()),)

    echoed: list[str] = []
    monkeypatch.setattr("code_factory.trackers.cli.typer.echo", echoed.append)
    _render_human({"issues": []})
    _render_human(
        {"issues": [{"identifier": "ENG-1", "title": "Fix", "state": {"name": "Todo"}}]}
    )
    assert echoed == ["ENG-1: Fix [Todo]"]


@pytest.mark.asyncio
async def test_remaining_repository_and_review_resolution_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def direct_upstream(_workspace: str, command: str) -> ShellResult:
        if "abbrev-ref" in command:
            return ShellResult(0, "origin/main\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.repository.repository_command", direct_upstream
    )
    assert await upstream_name("/repo") == "origin/main"

    async def direct_upstream_head(_workspace: str, command: str) -> ShellResult:
        if command == "git rev-parse @{upstream}":
            return ShellResult(0, "abc123\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.repository.repository_command", direct_upstream_head
    )
    assert await upstream_head_sha("/repo") == "abc123"

    async def missing_upstream_head(_workspace: str, command: str) -> ShellResult:
        if command == "git rev-parse @{upstream}":
            return ShellResult(1, "", "")
        if "branch.feature.remote" in command:
            return ShellResult(1, "", "")
        if "branch.feature.merge" in command:
            return ShellResult(0, "refs/heads/feature\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.repository.repository_command", missing_upstream_head
    )
    monkeypatch.setattr(
        "code_factory.workspace.repository.current_branch_name",
        lambda _workspace: asyncio.sleep(0, result="feature"),
    )
    assert await upstream_head_sha("/repo") is None

    async def empty_ls_remote(_workspace: str, command: str) -> ShellResult:
        if command == "git rev-parse @{upstream}":
            return ShellResult(1, "", "")
        if "branch.feature.remote" in command:
            return ShellResult(0, "origin\n", "")
        if "branch.feature.merge" in command:
            return ShellResult(0, "refs/heads/feature\n", "")
        if command.startswith("git ls-remote --exit-code"):
            return ShellResult(0, "", "")
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.repository.repository_command", empty_ls_remote
    )
    assert await upstream_head_sha("/repo") is None
    assert (
        _upstream_display_name("origin", "refs/remotes/upstream/main")
        == "upstream/main"
    )
    assert _upstream_display_name("origin", "refs/custom/feature") == "origin/feature"

    workflow_settings = make_snapshot(
        write_workflow_file(tmp_path / "REVIEW.md")
    ).settings
    closed: list[str] = []

    class FakeTracker:
        async def close(self) -> None:
            closed.append("closed")

    async def review_shell(
        command: str, *, cwd: str, env: dict[str, str] | None = None
    ) -> ShellResult:
        if command == "git fetch origin":
            return ShellResult(0, "", "")
        if command == "git symbolic-ref --quiet --short refs/remotes/origin/HEAD":
            return ShellResult(0, "origin/main\n", "")
        raise AssertionError(command)

    target = await resolve_review_target(
        "/repo",
        workflow_settings,
        "main",
        tracker_factory=lambda _settings: cast(Any, FakeTracker()),
        shell_capture=review_shell,
    )
    assert target.target == "main"
    assert closed == ["closed"]
    with pytest.raises(ReviewError, match="can't be blank"):
        await resolve_review_target(
            "/repo",
            workflow_settings,
            " ",
            tracker_factory=lambda _settings: cast(Any, FakeTracker()),
            shell_capture=review_shell,
        )

    assert (
        await resolve_repo_root(
            str(tmp_path / "WORKFLOW.md"),
            shell_capture=lambda command, *, cwd, env=None: asyncio.sleep(
                0, result=ShellResult(0, "/repo\n", "")
            ),
        )
        == "/repo"
    )

    with pytest.raises(ReviewError, match="boom"):
        await fetch_pull_request(
            "/repo",
            "feature",
            shell_capture=lambda command, *, cwd, env=None: asyncio.sleep(
                0, result=ShellResult(1, "", "boom")
            ),
        )
    assert trailing_ticket_number("ENG") is None


@pytest.mark.asyncio
async def test_remaining_runtime_and_review_runner_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "completion": {"require_pr": True},
                },
            },
        )
    )
    profile = snapshot.state_profile("In Progress")
    assert profile is not None
    issue = make_issue(identifier="ENG-1", branch_name="codex/eng-1")

    monkeypatch.setattr(
        "code_factory.runtime.worker.quality_gates.readiness.ensure_git_repository",
        lambda _workspace: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.quality_gates.readiness.current_branch_name",
        lambda _workspace: asyncio.sleep(0, result="codex/eng-1"),
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.quality_gates.readiness.worktree_status",
        lambda _workspace: asyncio.sleep(0, result=""),
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.quality_gates.readiness.upstream_name",
        lambda _workspace: asyncio.sleep(0, result="origin/codex/eng-1"),
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.quality_gates.readiness.head_sha",
        lambda _workspace: asyncio.sleep(0, result="abc123"),
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.quality_gates.readiness.upstream_head_sha",
        lambda _workspace: asyncio.sleep(0, result=None),
    )
    readiness = await native_readiness_result(str(tmp_path), issue, profile)
    assert readiness is not None
    assert "could not be resolved on remote" in readiness.stderr

    tracker_ops = SimpleNamespace(
        get_workpad=lambda _identifier: asyncio.sleep(0, result={"body": "   "}),
        close=lambda: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.workpad.build_tracker_ops",
        lambda settings, *, allowed_roots: tracker_ops,
    )
    assert (
        await _load_workpad_body(
            snapshot.settings, cast(Any, object()), issue, "/workspace"
        )
        is None
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "WORKPAD.md").write_text("body", encoding="utf-8")

    class CommentTracker:
        async def fetch_issue_comments(self, issue_id: str) -> list[IssueComment]:
            return [IssueComment(id=None, body="## Codex Workpad\nbody")]

    with pytest.raises(RuntimeError, match="missing_workpad_comment_id"):
        await sync_workspace_workpad(
            snapshot.settings,
            cast(Any, CommentTracker()),
            make_issue(identifier=None),
            str(workspace),
        )

    workflow = write_workflow_file(
        tmp_path / "REVIEW_RUN.md",
        review={"servers": [{"name": "web", "command": "run web"}]},
    )
    runner_calls: list[tuple[ReviewTarget, tuple[Any, ...]]] = []

    class FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(self, target: ReviewTarget, servers, **_kwargs: Any) -> None:
            runner_calls.append((target, tuple(servers)))

    monkeypatch.setattr(
        "code_factory.workspace.review.review_session.resolve_repo_root",
        lambda _workflow_path: asyncio.sleep(0, result="/repo"),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review.review_session.resolve_review_target",
        lambda repo_root, settings, target: asyncio.sleep(
            0,
            result=ReviewTarget(
                target="main",
                kind="main",
                ticket_identifier=None,
                ticket_number=None,
                ref="origin/main",
            ),
        ),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review.review_session.ReviewRunner", FakeRunner
    )
    monkeypatch.setattr(
        "code_factory.workspace.review.review_session._interactive_review_supported",
        lambda *_args, **_kwargs: False,
    )
    await run_review_session(str(workflow), "main", keep=True)
    assert runner_calls and runner_calls[0][0].target == "main"

    console, output = _review_console()
    review_runner = ReviewRunner(
        repo_root="/repo",
        worktree_root="/tmp/review",
        keep=False,
        prepare_command="prepare",
    )
    monkeypatch.setattr(
        "code_factory.workspace.review.review_runner.capture_shell",
        lambda command, *, cwd, env=None: asyncio.sleep(
            0, result=ShellResult(1, "", "boom")
        ),
    )
    with pytest.raises(ReviewError, match="Prepare command failed"):
        await review_runner._run_prepare(
            ReviewConsoleObserver(console),
            ReviewTarget("ENG-1", "ticket", "ENG-1", 1, "sha"),
            "/tmp/review/eng-1",
        )
    await review_runner._cleanup_worktree(
        ReviewConsoleObserver(console), "/tmp/review/eng-1"
    )
    assert "Failed to remove review worktree" in output.getvalue()

    async def invalid_reference_capture(
        command: str, *, cwd: str, env: dict[str, str] | None = None
    ) -> ShellResult:
        if command.startswith("git worktree add --detach"):
            return ShellResult(1, "", "invalid reference")
        if command.startswith("git fetch origin"):
            return ShellResult(1, "", "fetch failed")
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.review.review_runner.capture_shell",
        invalid_reference_capture,
    )
    with pytest.raises(ReviewError, match="Failed to create review worktree"):
        await _create_worktree(
            "/repo",
            str(tmp_path / "review" / "eng-1"),
            ReviewTarget("ENG-1", "ticket", "ENG-1", 1, "missing", branch_name="eng-1"),
        )

    monkeypatch.setattr(
        "code_factory.workspace.review.review_runner.capture_shell",
        lambda command, *, cwd, env=None: asyncio.sleep(
            0, result=ShellResult(0, "abc123\n", "")
        ),
    )
    assert await _head_sha("/tmp/review/eng-1") == "abc123"

    empty_entry = cast(
        Any,
        SimpleNamespace(
            target=SimpleNamespace(target="ENG-1"),
            launch=SimpleNamespace(name="web"),
            process=SimpleNamespace(process=SimpleNamespace(stdout=None, stderr=None)),
        ),
    )
    assert _log_tasks(empty_entry, ReviewConsoleObserver(console)) == []

    class FakeStream:
        def __init__(self) -> None:
            self._lines = [b"\n", b"hello\n", b""]

        async def readline(self) -> bytes:
            return self._lines.pop(0)

    stream_console, stream_output = _review_console()
    entry = cast(
        Any,
        SimpleNamespace(
            launch=SimpleNamespace(name="web"),
        ),
    )
    await _stream_output(
        ReviewConsoleObserver(stream_console), entry, FakeStream(), "stdout"
    )
    assert "hello" in stream_output.getvalue()

    async def fast_wait() -> int:
        return 0

    async def slow_wait() -> int:
        await asyncio.sleep(10)
        return 0

    running = [
        cast(
            Any,
            SimpleNamespace(
                target=SimpleNamespace(target="ENG-1"),
                launch=SimpleNamespace(name="web"),
                process=SimpleNamespace(wait=fast_wait),
            ),
        ),
        cast(
            Any,
            SimpleNamespace(
                target=SimpleNamespace(target="ENG-2"),
                launch=SimpleNamespace(name="web"),
                process=SimpleNamespace(wait=slow_wait),
            ),
        ),
    ]
    with pytest.raises(ReviewError, match="ENG-1:web exited."):
        await _wait_for_exit(running, stop_event=None)
    await _cancel_log_tasks([])


@pytest.mark.asyncio
async def test_remaining_dispatch_retry_and_recovery_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    issue = make_issue(state="Todo")

    dispatch = _DispatchCoverage()
    dispatch.settings = workflow.settings
    dispatch.workflow_snapshot = workflow
    dispatch.claimed = set()
    dispatch.running = {}
    dispatch.retry_entries = {}
    dispatch.tracker = SimpleNamespace(
        update_issue_state=lambda issue_id, state: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
    )
    dispatch._schedule_issue_retry_calls: list[tuple[Any, ...]] = []
    dispatch._released: list[str] = []
    dispatch._schedule_issue_retry = (
        lambda *args, **kwargs: dispatch._schedule_issue_retry_calls.append(
            (args, kwargs)
        )
    )
    dispatch._release_issue_claim = lambda issue_id: dispatch._released.append(issue_id)

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.transition_issue_to_failure_state",
        lambda *args, **kwargs: asyncio.sleep(0),
    )
    await dispatch._dispatch_auto_issue(issue, attempt=3)
    assert dispatch._released == ["issue-1"]

    dispatch._released.clear()
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.transition_issue_to_failure_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("escalation failed")
        ),
    )
    await dispatch._dispatch_auto_issue(issue, attempt=3)
    assert dispatch._schedule_issue_retry_calls[-1][1]["error"].startswith(
        "failure escalation failed"
    )

    retrying = _RetryCoverage()
    retrying.settings = workflow.settings
    retrying.workflow_snapshot = workflow
    retrying.claimed = set()
    retrying.retry_entries = {}
    retrying.tracker = cast(Any, object())
    retrying._released: list[str] = []
    retrying._scheduled: list[tuple[Any, ...]] = []
    retrying._release_issue_claim = lambda issue_id: retrying._released.append(issue_id)
    retrying._schedule_issue_retry = lambda *args, **kwargs: retrying._scheduled.append(
        (args, kwargs)
    )
    retry_entry = RetryEntry(
        issue_id="issue-1",
        identifier="ENG-1",
        attempt=4,
        due_at_ms=0,
        token="token",
        state_name="In Progress",
    )

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.retrying.transition_issue_to_failure_state",
        lambda *args, **kwargs: asyncio.sleep(0),
    )
    await retrying._handle_retry_entry(retry_entry)
    assert retrying._released == ["issue-1"]

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.retrying.transition_issue_to_failure_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("escalation failed")
        ),
    )
    await retrying._handle_retry_entry(retry_entry)
    assert retrying._scheduled[-1][1]["error"].startswith("failure escalation failed")

    recovery = _RecoveryCoverage()
    recovery.settings = workflow.settings
    recovery.workflow_snapshot = workflow
    recovery.completed = set()
    recovery.tracker = cast(Any, object())
    recovery._scheduled: list[tuple[Any, ...]] = []
    recovery._release_issue_claim = lambda issue_id: None
    recovery._schedule_issue_retry = lambda *args, **kwargs: recovery._scheduled.append(
        (args, kwargs)
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.recovery.transition_issue_to_failure_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("escalation failed")
        ),
    )
    await recovery._retry_or_escalate_worker_exit(
        SimpleNamespace(
            issue=issue,
            issue_id="issue-1",
            identifier="ENG-1",
            workspace_path="/tmp/workspace",
        ),
        attempt=4,
        error="boom",
    )
    assert recovery._scheduled[-1][1]["error"].startswith("failure escalation failed")
