from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import code_factory.runtime.worker.readiness as readiness_module
from code_factory.issues import IssueComment
from code_factory.runtime.messages import (
    AgentWorkerUpdate,
    RefreshRequest,
    Shutdown,
    SnapshotRequest,
    WorkerCleanupComplete,
    WorkerExited,
    WorkflowReloadError,
    WorkflowUpdated,
)
from code_factory.runtime.orchestration.actor import OrchestratorActor
from code_factory.runtime.orchestration.models import RetryEntry, RunningEntry
from code_factory.runtime.orchestration.policy import (
    active_issue_state,
    available_slots,
    candidate_issue,
    dispatch_sort_key,
    failure_retry_delay,
    next_retry_attempt,
    normalize_retry_attempt,
    sort_issues_for_dispatch,
    state_slots_available,
    terminal_issue_state,
    todo_issue_blocked_by_non_terminal,
)
from code_factory.runtime.orchestration.snapshot import snapshot_payload
from code_factory.runtime.orchestration.tokens import (
    apply_token_delta,
    compute_token_delta,
    extract_rate_limits,
    extract_token_delta,
    extract_token_usage,
    get_token_usage,
    integer_like,
)
from code_factory.runtime.subprocess.process_tree import ProcessTree
from code_factory.runtime.worker.actor import IssueWorker
from code_factory.runtime.worker.completion import (
    before_complete_feedback_prompt,
    before_complete_update,
    emit_before_complete_update,
)
from code_factory.runtime.worker.readiness import native_readiness_result
from code_factory.runtime.worker.results import (
    build_prompt_issue_data,
    parse_results_by_state,
    persist_state_result,
)
from code_factory.runtime.worker.utils import tracker_state_is_active
from code_factory.runtime.worker.workpad import (
    DEFAULT_WORKPAD_BODY,
    WORKPAD_HEADER,
    hydrate_workspace_workpad,
    sync_workspace_workpad,
)
from code_factory.structured_results import StructuredTurnResult
from code_factory.trackers.memory import MemoryTracker
from code_factory.workspace.hooks import HookCommandResult
from code_factory.workspace.manager import WorkspaceManager
from code_factory.workspace.review_resolution import ReviewError
from code_factory.workspace.workpad import WORKPAD_FILENAME, workspace_workpad_path

from .conftest import make_issue, make_snapshot, write_workflow_file


def make_actor(
    tmp_path: Path, *, workflow_overrides: dict[str, Any] | None = None
) -> OrchestratorActor:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md", **(workflow_overrides or {})
    )
    snapshot = make_snapshot(workflow)
    return OrchestratorActor(
        snapshot, tracker_factory=lambda settings: MemoryTracker([])
    )


@pytest.fixture(autouse=True)
def patch_issue_worker_workpad(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.hydrate_workspace_workpad", _noop
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.sync_workspace_workpad", _noop
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.prepare_workspace_repository", _noop
    )


@pytest.mark.asyncio
async def test_runtime_policy_and_snapshot_helpers(tmp_path: Path) -> None:
    actor = make_actor(
        tmp_path,
        workflow_overrides={
            "agent": {
                "max_concurrent_agents": 2,
                "max_concurrent_agents_by_state": {"Todo": 1},
            }
        },
    )
    issue = make_issue(id="issue-1", identifier="ENG-1", state="Todo", priority=2)
    blocker = make_issue(id="issue-2", identifier="ENG-2", state="Done")
    issue_with_blocker = make_issue(
        id="issue-3",
        identifier="ENG-3",
        state="Todo",
        blocked_by=blocker.blocked_by + (SimpleNamespace(state="In Progress"),),  # type: ignore[operator]
    )

    assert dispatch_sort_key(issue)[0] == 2
    assert (
        sort_issues_for_dispatch([make_issue(priority=4, identifier="z"), issue])[0]
        == issue
    )
    assert candidate_issue(actor.settings, issue) is True
    assert candidate_issue(actor.settings, make_issue(id=None)) is False
    assert (
        todo_issue_blocked_by_non_terminal(actor.settings, issue_with_blocker) is True
    )
    assert terminal_issue_state(actor.settings, "Done") is True
    assert active_issue_state(actor.settings, "In Progress") is True
    running_entry = RunningEntry(
        issue_id="issue-1",
        identifier="ENG-1",
        issue=issue,
        workspace_path="/tmp/workspace",
        worker=object(),
        started_at=datetime.now(UTC),
    )
    assert (
        state_slots_available(actor.settings, {"issue-1": running_entry}, issue)
        is False
    )
    assert available_slots(actor.settings, {}) == 2
    assert normalize_retry_attempt(None) == 0
    assert normalize_retry_attempt(2) == 2
    assert (
        next_retry_attempt(
            RunningEntry(
                "x", "X", issue, "/tmp", object(), datetime.now(UTC), retry_attempt=2
            )
        )
        == 3
    )
    assert failure_retry_delay(100, 500, 4) == 500

    retry_entry = RetryEntry("issue-1", "ENG-1", 2, 1_000, "token", error="boom")
    payload = snapshot_payload(
        {"issue-1": running_entry},
        {"issue-1": retry_entry},
        workflow_snapshot=actor.workflow_snapshot,
        agent_totals={"total_tokens": 3},
        rate_limits={"primary": {}},
        poll_check_in_progress=True,
        next_poll_due_at_ms=500,
        poll_interval_ms=200,
        now_ms=100,
    )
    assert payload["running"][0]["issue_id"] == "issue-1"
    assert payload["retrying"][0]["attempt"] == 2
    assert payload["workflow"]["agent"]["max_concurrent_agents"] == 2
    assert payload["polling"]["next_poll_in_ms"] == 400
    assert actor.snapshot_now()["polling"]["poll_interval_ms"] == 30_000
    assert actor.snapshot_now()["workflow"]["version"] == 1
    assert (
        snapshot_payload(
            {},
            {},
            workflow_snapshot=None,
            agent_totals={},
            rate_limits=None,
            poll_check_in_progress=False,
            next_poll_due_at_ms=None,
            poll_interval_ms=200,
            now_ms=100,
        )["workflow"]["reload_error"]
        is None
    )


def test_runtime_token_helpers() -> None:
    entry = RunningEntry(
        issue_id="issue-1",
        identifier="ENG-1",
        issue=make_issue(),
        workspace_path="/tmp/workspace",
        worker=object(),
        started_at=datetime.now(UTC),
    )
    delta = extract_token_delta(
        entry,
        {"token_usage": {"inputTokens": "2", "output_tokens": 3, "total": 5}},
    )
    assert delta["input_tokens"] == 2
    assert delta["output_tokens"] == 3
    assert delta["total_tokens"] == 5
    assert extract_token_usage({"token_usage": {"x": 1}}) == {"x": 1}
    assert extract_token_usage({}) == {}
    assert extract_rate_limits({"rate_limits": {"primary": {}}}) == {"primary": {}}
    assert extract_rate_limits({"rate_limits": []}) is None
    assert compute_token_delta(5, 10) == (5, 10)
    assert compute_token_delta(5, 3) == (0, 5)
    assert get_token_usage({"promptTokens": "7"}, "input") == 7
    assert get_token_usage({"completion": 4}, "output") == 4
    assert get_token_usage({"totalTokens": 11}, "total") == 11
    assert integer_like(" 8 ") == 8
    assert integer_like(-1) is None
    assert (
        apply_token_delta({"input_tokens": 1}, {"input_tokens": -5})["input_tokens"]
        == 0
    )


@pytest.mark.asyncio
async def test_process_tree_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 42
            self.returncode = 0
            self.communicated: list[int | None] = []

        async def wait(self) -> int:
            return 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicated.append(None)
            return b"out", b"err"

    fake_process = FakeProcess()
    tree = ProcessTree(process=cast(Any, fake_process), command="cmd", cwd="/tmp")
    assert tree.pid == 42
    assert await tree.wait() == 0
    assert await tree.communicate() == (b"out", b"err")
    assert await tree.capture_output(100) == (0, "outerr")

    class ReturnProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = 1

    await ProcessTree(
        process=cast(Any, ReturnProcess()), command="cmd", cwd="/tmp"
    ).terminate()

    class TerminatingProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = None
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    terminating = TerminatingProcess()
    tree = ProcessTree(process=cast(Any, terminating), command="cmd", cwd="/tmp")
    monkeypatch.setattr("code_factory.runtime.subprocess.process_tree.os.name", "nt")
    await tree.terminate()
    assert terminating.terminated is True


@pytest.mark.asyncio
async def test_workspace_workpad_helpers_fallback_paths(tmp_path: Path) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        tracker={"kind": "memory"},
    )
    settings = make_snapshot(workflow).settings
    issue = make_issue(id="issue-1", identifier="ENG-1")
    tracker = MemoryTracker([issue])
    workspace = tmp_path / "workspace"

    path = await hydrate_workspace_workpad(settings, tracker, issue, str(workspace))
    assert path == workspace_workpad_path(str(workspace))
    assert Path(path).read_text(encoding="utf-8") == DEFAULT_WORKPAD_BODY

    existing_body = f"{WORKPAD_HEADER}\n\nexisting\n"
    await tracker.create_comment(issue.id or "", existing_body)
    hydrated = await hydrate_workspace_workpad(settings, tracker, issue, str(workspace))
    assert Path(hydrated).read_text(encoding="utf-8") == existing_body

    Path(hydrated).write_text(f"{WORKPAD_HEADER}\n\nupdated\n", encoding="utf-8")
    await sync_workspace_workpad(settings, tracker, issue, str(workspace))
    comments = await tracker.fetch_issue_comments(issue.id or "")
    assert len(comments) == 1
    assert comments[0].body == f"{WORKPAD_HEADER}\n\nupdated\n"

    new_issue = make_issue(id="issue-2", identifier="ENG-2")
    tracker.replace_issues([issue, new_issue])
    await hydrate_workspace_workpad(settings, tracker, new_issue, str(workspace))
    Path(workspace_workpad_path(str(workspace))).write_text(
        f"{WORKPAD_HEADER}\n\nfresh\n", encoding="utf-8"
    )
    await sync_workspace_workpad(settings, tracker, new_issue, str(workspace))
    fresh_comments = await tracker.fetch_issue_comments(new_issue.id or "")
    assert len(fresh_comments) == 1
    assert fresh_comments[0].body == f"{WORKPAD_HEADER}\n\nfresh\n"

    with pytest.raises(RuntimeError, match="missing_issue_id_for_workpad_sync"):
        await sync_workspace_workpad(
            settings,
            tracker,
            make_issue(id=None, identifier="ENG-3"),
            str(workspace),
        )


@pytest.mark.asyncio
async def test_workspace_workpad_helpers_linear_ops_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md")).settings
    issue = make_issue(id="issue-1", identifier="ENG-1")
    tracker = MemoryTracker([issue])
    workspace = tmp_path / "workspace"
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeTrackerOps:
        async def get_workpad(self, issue_identifier: str) -> dict[str, str]:
            calls.append(("get_workpad", {"issue": issue_identifier}))
            return {"body": "hydrated body\n"}

        async def sync_workpad(
            self,
            issue_identifier: str,
            *,
            body: str | None = None,
            file_path: str | None = None,
        ) -> dict[str, str]:
            calls.append(
                (
                    "sync_workpad",
                    {
                        "issue": issue_identifier,
                        "body": body,
                        "file_path": file_path,
                    },
                )
            )
            return {"comment_id": "workpad-1"}

        async def close(self) -> None:
            calls.append(("close", {}))

    monkeypatch.setattr(
        "code_factory.runtime.worker.workpad.build_tracker_ops",
        lambda *_args, **_kwargs: FakeTrackerOps(),
    )

    hydrated = await hydrate_workspace_workpad(settings, tracker, issue, str(workspace))
    assert Path(hydrated).read_text(encoding="utf-8") == "hydrated body\n"
    assert calls == [("get_workpad", {"issue": "ENG-1"}), ("close", {})]

    calls.clear()
    Path(hydrated).write_text("updated body\n", encoding="utf-8")
    await sync_workspace_workpad(settings, tracker, issue, str(workspace))
    assert calls == [
        (
            "sync_workpad",
            {"issue": "ENG-1", "body": None, "file_path": WORKPAD_FILENAME},
        ),
        ("close", {}),
    ]


@pytest.mark.asyncio
async def test_issue_worker_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    snapshot = make_snapshot(workflow)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    issue = make_issue(id="issue-1", identifier="ENG-1")

    class FakeSession:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1

    class FakeRuntime:
        def __init__(self) -> None:
            self.session = FakeSession()
            self.prompts: list[str] = []

        async def start_session(self, workspace: str) -> FakeSession:
            return self.session

        async def run_turn(
            self,
            session: Any,
            prompt: str,
            issue: Any,
            *,
            on_message=None,
            output_schema=None,
        ) -> StructuredTurnResult:
            self.prompts.append(prompt)
            if on_message is not None:
                await on_message(
                    {"event": "notification", "timestamp": datetime.now(UTC)}
                )
            return StructuredTurnResult(
                decision="transition",
                summary="done",
                next_state="Done",
            )

    class FakeManager:
        def __init__(self) -> None:
            self.after_run_calls = 0

        async def create_for_issue(self, issue: Any) -> Any:
            return SimpleNamespace(path=str(tmp_path / "workspace"))

        async def run_before_run_hook(self, workspace: str, issue: Any) -> None:
            return None

        async def run_after_run_hook(self, workspace: str, issue: Any) -> None:
            self.after_run_calls += 1

    tracker = MemoryTracker(
        [make_issue(id="issue-1", identifier="ENG-1", state="In Progress")]
    )
    worker = IssueWorker(
        issue=issue,
        workflow_snapshot=snapshot,
        orchestrator_queue=queue,
        tracker=tracker,
    )
    fake_runtime = FakeRuntime()
    worker.workspace_manager = FakeManager()  # type: ignore[assignment]
    worker._agent_runtime = fake_runtime  # type: ignore[assignment]
    await worker.run()
    update = await queue.get()
    exited = await queue.get()
    assert isinstance(update, AgentWorkerUpdate)
    assert isinstance(exited, WorkerExited)
    assert exited.normal is True
    assert await worker._refresh_issue_state(make_issue(id=None)) is None

    stop_worker = IssueWorker(
        issue=issue,
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=tracker,
    )
    stop_worker._session = fake_runtime.session  # type: ignore[assignment]
    await stop_worker.stop("stop")
    assert stop_worker.stop_event.is_set()

    idle_worker = IssueWorker(
        issue=issue,
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=tracker,
    )
    await idle_worker.stop("stop")
    assert idle_worker.stop_event.is_set()


@pytest.mark.asyncio
async def test_issue_worker_state_edge_paths_and_result_helpers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "failure_state": "Blocked",
                },
            },
        )
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    tracker = MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")])
    workspace_path = str(tmp_path / "workspace")
    worker = IssueWorker(
        issue=make_issue(id="issue-1", identifier="ENG-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=queue,
        tracker=tracker,
    )

    class Session:
        async def stop(self) -> None:
            return None

    class FakeRuntime:
        def __init__(self, result: StructuredTurnResult) -> None:
            self.result = result
            self.output_schema = None

        async def start_session(self, workspace: str) -> Session:
            return Session()

        async def run_turn(
            self,
            session: Any,
            prompt: str,
            issue: Any,
            *,
            on_message=None,
            output_schema=None,
        ) -> StructuredTurnResult:
            self.output_schema = output_schema
            return self.result

    stop_worker = IssueWorker(
        issue=make_issue(id="issue-2", identifier="ENG-2"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=tracker,
    )
    stop_worker.stop_event.set()
    await stop_worker._run_state(cast(Any, object()))

    auto_worker = IssueWorker(
        issue=make_issue(id="issue-1", identifier="ENG-1", state="Todo"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=MemoryTracker(
            [make_issue(id="issue-1", identifier="ENG-1", state="Todo")]
        ),
    )
    with pytest.raises(RuntimeError, match="worker_requires_agent_run_state"):
        await auto_worker._run_state(cast(Any, object()))

    with pytest.raises(RuntimeError, match="missing_state_profile"):
        worker._target_state(make_issue(state="Review"), "transition", "Done")
    with pytest.raises(RuntimeError, match="missing_next_state_for_transition"):
        worker._target_state(make_issue(), "transition", None)
    no_failure_snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "NO_FAILURE_WORKFLOW.md",
            states={"In Progress": {"prompt": "default"}},
        )
    )
    no_failure_worker = IssueWorker(
        issue=make_issue(id="issue-9", identifier="ENG-9"),
        workflow_snapshot=no_failure_snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=tracker,
    )
    assert no_failure_worker._target_state(make_issue(), "blocked", None) == (
        no_failure_snapshot.settings.failure_state
    )
    with pytest.raises(RuntimeError, match="unsupported_turn_decision"):
        worker._target_state(make_issue(), "continue", "Done")
    with pytest.raises(RuntimeError, match="next_state_must_not_equal_current_state"):
        worker._target_state(
            make_issue(state="In Progress"), "transition", "in progress"
        )

    invalid_worker = IssueWorker(
        issue=make_issue(id="issue-1", identifier="ENG-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=tracker,
    )
    invalid_worker._agent_runtime = FakeRuntime(
        StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Review",
        )
    )  # type: ignore[assignment]
    invalid_worker.workspace_path = workspace_path
    with pytest.raises(RuntimeError, match="invalid_next_state"):
        await invalid_worker._run_state(Session())

    class MissingIdTracker(MemoryTracker):
        async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Any]:
            return [make_issue(id=None, identifier="ENG-1", state="In Progress")]

    missing_id_worker = IssueWorker(
        issue=make_issue(id="issue-1", identifier="ENG-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=MissingIdTracker([]),
    )
    missing_id_worker._agent_runtime = FakeRuntime(
        StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        )
    )  # type: ignore[assignment]
    missing_id_worker.workspace_path = workspace_path
    with pytest.raises(RuntimeError, match="missing_issue_id_for_state_transition"):
        await missing_id_worker._run_state(Session())
    assert worker._target_state(make_issue(), "blocked", "Other") == "Blocked"
    schema_tracker = MemoryTracker([make_issue(id="issue-3", identifier="ENG-3")])
    schema_worker = IssueWorker(
        issue=make_issue(id="issue-3", identifier="ENG-3"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=schema_tracker,
    )
    schema_runtime = FakeRuntime(
        StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        )
    )
    schema_worker._agent_runtime = schema_runtime  # type: ignore[assignment]
    schema_worker.workspace_path = workspace_path
    await schema_worker._run_state(Session())
    allowed_schema = schema_runtime.output_schema
    assert allowed_schema is not None
    assert allowed_schema["properties"]["next_state"] == {"enum": ["Done", None]}

    blocked_tracker = MemoryTracker([make_issue(id="issue-4", identifier="ENG-4")])
    blocked_worker = IssueWorker(
        issue=make_issue(id="issue-4", identifier="ENG-4"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=blocked_tracker,
    )
    blocked_worker._agent_runtime = FakeRuntime(
        StructuredTurnResult(
            decision="blocked",
            summary="blocked",
            next_state="Elsewhere",
        )
    )  # type: ignore[assignment]
    blocked_worker.workspace_path = workspace_path
    await blocked_worker._run_state(Session())
    blocked_issue = await blocked_tracker.fetch_issue_states_by_ids(["issue-4"])
    assert blocked_issue[0].state == "Blocked"
    missing_workspace_worker = IssueWorker(
        issue=make_issue(id="issue-5", identifier="ENG-5"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=MemoryTracker([make_issue(id="issue-5", identifier="ENG-5")]),
    )
    missing_workspace_worker._agent_runtime = FakeRuntime(
        StructuredTurnResult(
            decision="blocked",
            summary="blocked",
            next_state="Elsewhere",
        )
    )  # type: ignore[assignment]
    missing_workspace_worker.workspace_path = None
    with pytest.raises(RuntimeError, match="missing_workspace_for_workpad_sync"):
        await missing_workspace_worker._run_state(Session())

    stop_after_refresh_worker = IssueWorker(
        issue=make_issue(id="issue-1", identifier="ENG-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=tracker,
    )
    stop_after_refresh_worker._agent_runtime = FakeRuntime(
        StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        )
    )  # type: ignore[assignment]

    async def refresh_and_stop(issue: Any) -> Any:
        stop_after_refresh_worker.stop_event.set()
        return make_issue(id="issue-1", identifier="ENG-1", state="In Progress")

    stop_after_refresh_worker._refresh_issue_state = refresh_and_stop  # type: ignore[method-assign]
    await stop_after_refresh_worker._run_state(Session())

    class CleanupManager:
        async def create_for_issue(self, issue: Any) -> Any:
            return SimpleNamespace(path=str(tmp_path / "workspace"))

        async def run_before_run_hook(self, workspace: str, issue: Any) -> None:
            return None

        async def run_after_run_hook(self, workspace: str, issue: Any) -> None:
            raise RuntimeError("after-run failed")

        async def remove(self, workspace: str) -> list[str]:
            raise RuntimeError("remove failed")

    cleanup_worker = IssueWorker(
        issue=make_issue(id="issue-1", identifier="ENG-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")]),
    )
    cleanup_worker.workspace_manager = CleanupManager()  # type: ignore[assignment]
    cleanup_worker._agent_runtime = FakeRuntime(
        StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        )
    )  # type: ignore[assignment]
    with caplog.at_level("ERROR"):
        await cleanup_worker.run()
    exited = await cleanup_worker.queue.get()
    assert isinstance(exited, WorkerExited)
    assert exited.completed is True
    assert any(
        "after_run hook cleanup failed" in record.message for record in caplog.records
    )
    assert any(
        "workspace removal failed" in record.message for record in caplog.records
    )

    issue_without_id = make_issue(id=None)
    await persist_state_result(
        tracker,
        issue_without_id,
        "In Progress",
        StructuredTurnResult(decision="transition", summary="done", next_state="Done"),
    )
    assert await tracker.fetch_issue_comments("issue-1") == []

    issue = make_issue(id="issue-1", identifier="ENG-1", blocked_by=())
    issue_data = await build_prompt_issue_data(tracker, issue)
    assert issue_data["upstream_tickets"] == []

    upstream = make_issue(id="up-1", identifier="ENG-UP", state="Done")
    dependent = make_issue(
        id="dep-1",
        identifier="ENG-DEP",
        blocked_by=(
            replace(upstream, blocked_by=()),
            make_issue(id=None),
        ),
    )
    tracker = MemoryTracker([upstream])
    await tracker.create_comment(
        "up-1",
        "## Code Factory Result: Review\n\nversion: 1\ndecision: transition\nnext_state: Done\nsummary: |\n  done\n",
    )
    await tracker.create_comment(
        "up-1",
        "## Code Factory Result: Review\n\nversion: 1\ndecision: nope\nsummary: |\n  bad\n",
    )
    with pytest.raises(RuntimeError, match="malformed_state_result_comment"):
        await build_prompt_issue_data(tracker, dependent)
    await persist_state_result(
        tracker,
        replace(upstream, blocked_by=()),
        "Review",
        StructuredTurnResult(
            decision="transition",
            summary="updated",
            next_state="Done",
        ),
    )
    assert len(await tracker.fetch_issue_comments("up-1")) == 2
    missing_comment_id_tracker = MemoryTracker([upstream])
    missing_comment_id_tracker._comments_by_issue["up-1"] = [
        IssueComment(
            id=None,
            body="## Code Factory Result: Review\n\nversion: 1\ndecision: transition\nnext_state: Done\nsummary: |\n  ok\n",
        )
    ]
    with pytest.raises(RuntimeError, match="missing_state_result_comment_id"):
        await persist_state_result(
            missing_comment_id_tracker,
            replace(upstream, blocked_by=()),
            "Review",
            StructuredTurnResult(
                decision="transition",
                summary="updated",
                next_state="Done",
            ),
        )

    class MissingUpstreamIdTracker(MemoryTracker):
        async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Any]:
            return [make_issue(id=None, identifier="ENG-UP", state="Done")]

    missing_upstream_data = await build_prompt_issue_data(
        MissingUpstreamIdTracker([]),
        make_issue(
            id="dep-2",
            identifier="ENG-DEP-2",
            blocked_by=(make_issue(id="up-2", identifier="ENG-UP-2"),),
        ),
    )
    assert missing_upstream_data["upstream_tickets"] == []

    parsed_results = parse_results_by_state(
        [
            IssueComment(
                id="1",
                body="## Code Factory Result: Review\n\nversion: 1\ndecision: transition\nnext_state: Done\nsummary: |\n  ok\n",
            ),
            IssueComment(id="3", body="ordinary comment"),
        ],
        ticket_label="ENG-UP",
    )
    assert parsed_results["Review"]["next_state"] == "Done"
    with pytest.raises(RuntimeError, match="malformed_state_result_comment"):
        parse_results_by_state(
            [
                IssueComment(
                    id="2",
                    body="## Code Factory Result: Review\n\nversion: 1\ndecision: nope\nsummary: |\n  bad\n",
                )
            ],
            ticket_label="ENG-UP",
        )
    assert tracker_state_is_active(snapshot.settings, "In Progress") is True
    assert tracker_state_is_active(snapshot.settings, "Done") is False


@pytest.mark.asyncio
async def test_issue_worker_before_complete_hook_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "hooks": {
                        "before_complete": "uv run pytest -q",
                        "before_complete_max_feedback_loops": 1,
                    },
                },
            },
        )
    )

    class Session:
        async def stop(self) -> None:
            return None

    class FakeRuntime:
        def __init__(self, results: list[StructuredTurnResult]) -> None:
            self.results = list(results)
            self.prompts: list[str] = []
            self.sessions: list[Any] = []

        async def start_session(self, workspace: str) -> Session:
            return Session()

        async def steer(self, session: Any, message: str) -> str | None:
            return None

        async def run_turn(
            self,
            session: Any,
            prompt: str,
            issue: Any,
            *,
            on_message=None,
            output_schema=None,
        ) -> StructuredTurnResult:
            self.sessions.append(session)
            self.prompts.append(prompt)
            return self.results.pop(0)

    async def run_worker(
        *,
        tracker: MemoryTracker,
        runtime: FakeRuntime,
    ) -> IssueWorker:
        worker = IssueWorker(
            issue=make_issue(id="issue-1", identifier="ENG-1"),
            workflow_snapshot=snapshot,
            orchestrator_queue=asyncio.Queue(),
            tracker=tracker,
        )
        worker._agent_runtime = runtime  # type: ignore[assignment]
        worker.workspace_path = str(tmp_path / "workspace")
        return worker

    hook_calls: list[dict[str, Any]] = []
    hook_results: list[HookCommandResult] = [
        HookCommandResult(status=2, stdout="lint running\n", stderr="fix lint\n"),
        HookCommandResult(status=0, stdout="all green\n", stderr=""),
    ]

    async def fake_hook_command(
        settings: Any,
        command: str,
        workspace: str,
        issue_context: dict[str, str | None],
        hook_name: str,
        *,
        env: dict[str, str | None] | None = None,
    ) -> HookCommandResult:
        hook_calls.append(
            {
                "command": command,
                "workspace": workspace,
                "issue_context": issue_context,
                "hook_name": hook_name,
                "env": env,
            }
        )
        return hook_results.pop(0)

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_hook_command", fake_hook_command
    )

    retry_tracker = MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")])
    retry_runtime = FakeRuntime(
        [
            StructuredTurnResult(
                decision="transition",
                summary="first pass",
                next_state="Done",
            ),
            StructuredTurnResult(
                decision="transition",
                summary="second pass",
                next_state="Done",
            ),
        ]
    )
    retry_worker = await run_worker(tracker=retry_tracker, runtime=retry_runtime)
    await retry_worker._run_state(Session())
    updated_issue = await retry_tracker.fetch_issue_states_by_ids(["issue-1"])
    assert updated_issue[0].state == "Done"
    assert hook_calls[0]["hook_name"] == "before_complete"
    assert hook_calls[0]["env"] == {
        "CF_ISSUE_STATE": "In Progress",
        "CF_RESULT_DECISION": "transition",
        "CF_RESULT_NEXT_STATE": "Done",
    }
    assert len(retry_runtime.prompts) == 2
    assert "fix lint" in retry_runtime.prompts[1]
    assert retry_runtime.sessions[0] is retry_runtime.sessions[1]

    warned_results = [HookCommandResult(status=1, stdout="", stderr="tests flaky\n")]

    async def fake_warn_hook(
        settings: Any,
        command: str,
        workspace: str,
        issue_context: dict[str, str | None],
        hook_name: str,
        *,
        env: dict[str, str | None] | None = None,
    ) -> HookCommandResult:
        return warned_results.pop(0)

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_hook_command", fake_warn_hook
    )
    warned_tracker = MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")])
    warned_runtime = FakeRuntime(
        [StructuredTurnResult(decision="transition", summary="warn", next_state="Done")]
    )
    warned_worker = await run_worker(tracker=warned_tracker, runtime=warned_runtime)
    with caplog.at_level("WARNING"):
        await warned_worker._run_state(Session())
    warned_issue = await warned_tracker.fetch_issue_states_by_ids(["issue-1"])
    assert warned_issue[0].state == "Done"
    assert any(
        "before_complete hook failed but completion will continue" in record.message
        for record in caplog.records
    )

    blocked_hook_calls: list[str] = []

    async def fake_blocked_hook(*args: Any, **kwargs: Any) -> HookCommandResult:
        blocked_hook_calls.append("called")
        return HookCommandResult(status=0, stdout="", stderr="")

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_hook_command", fake_blocked_hook
    )
    blocked_tracker = MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")])
    blocked_runtime = FakeRuntime(
        [StructuredTurnResult(decision="blocked", summary="blocked", next_state="Done")]
    )
    blocked_worker = await run_worker(tracker=blocked_tracker, runtime=blocked_runtime)
    await blocked_worker._run_state(Session())
    blocked_issue = await blocked_tracker.fetch_issue_states_by_ids(["issue-1"])
    assert blocked_issue[0].state == snapshot.settings.failure_state
    assert blocked_hook_calls == []

    exhausted_results = [HookCommandResult(status=2, stdout="", stderr="still broken")]

    async def fake_exhausted_hook(
        settings: Any,
        command: str,
        workspace: str,
        issue_context: dict[str, str | None],
        hook_name: str,
        *,
        env: dict[str, str | None] | None = None,
    ) -> HookCommandResult:
        return exhausted_results[0]

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_hook_command",
        fake_exhausted_hook,
    )
    exhausted_tracker = MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")])
    exhausted_runtime = FakeRuntime(
        [
            StructuredTurnResult(
                decision="transition",
                summary="fail",
                next_state="Done",
            ),
            StructuredTurnResult(
                decision="transition",
                summary="still failing",
                next_state="Done",
            ),
        ]
    )
    exhausted_worker = await run_worker(
        tracker=exhausted_tracker, runtime=exhausted_runtime
    )
    await exhausted_worker._run_state(Session())
    exhausted_issue = await exhausted_tracker.fetch_issue_states_by_ids(["issue-1"])
    assert exhausted_issue[0].state == snapshot.settings.failure_state

    missing_workspace_worker = await run_worker(
        tracker=MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")]),
        runtime=FakeRuntime(
            [
                StructuredTurnResult(
                    decision="transition", summary="done", next_state="Done"
                )
            ]
        ),
    )
    missing_workspace_worker.workspace_path = None
    with pytest.raises(RuntimeError, match="missing_workspace_for_before_complete"):
        await missing_workspace_worker._run_state(Session())


@pytest.mark.asyncio
async def test_issue_worker_native_readiness_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "completion": {"require_pushed_head": True, "require_pr": True},
                    "hooks": {"before_complete": "uv run pytest -q"},
                },
            },
        )
    )

    class Session:
        async def stop(self) -> None:
            return None

    class FakeRuntime:
        def __init__(self, results: list[StructuredTurnResult]) -> None:
            self.results = list(results)
            self.prompts: list[str] = []

        async def run_turn(
            self,
            session: Any,
            prompt: str,
            issue: Any,
            *,
            on_message=None,
            output_schema=None,
        ) -> StructuredTurnResult:
            self.prompts.append(prompt)
            return self.results.pop(0)

    native_results = [
        HookCommandResult(status=2, stdout="", stderr="push your branch"),
        HookCommandResult(status=0, stdout="ok", stderr=""),
    ]
    hook_calls: list[str] = []

    async def fake_native(*_args: Any, **_kwargs: Any) -> HookCommandResult | None:
        return native_results.pop(0)

    async def fake_hook_command(
        settings: Any,
        command: str,
        workspace: str,
        issue_context: dict[str, str | None],
        hook_name: str,
        *,
        env: dict[str, str | None] | None = None,
    ) -> HookCommandResult:
        hook_calls.append(hook_name)
        return HookCommandResult(status=0, stdout="hook ok", stderr="")

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.native_readiness_result",
        fake_native,
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_hook_command", fake_hook_command
    )

    worker = IssueWorker(
        issue=make_issue(id="issue-1", identifier="ENG-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")]),
    )
    worker.workspace_path = str(tmp_path / "workspace")
    worker._agent_runtime = FakeRuntime(
        [
            StructuredTurnResult(
                decision="transition",
                summary="first",
                next_state="Done",
            ),
            StructuredTurnResult(
                decision="transition",
                summary="second",
                next_state="Done",
            ),
        ]
    )  # type: ignore[assignment]

    await worker._run_state(Session())
    updated_issue = await worker.tracker.fetch_issue_states_by_ids(["issue-1"])
    assert updated_issue[0].state == "Done"
    assert hook_calls == ["before_complete"]
    assert "push your branch" in worker._agent_runtime.prompts[1]  # type: ignore[union-attr]

    updates: list[AgentWorkerUpdate] = []
    while not worker.queue.empty():
        update = await worker.queue.get()
        assert isinstance(update, AgentWorkerUpdate)
        updates.append(update)
    assert updates[0].update["event"] == "before_complete_blocked"
    assert updates[0].update["gate_source"] == "native"
    assert updates[0].update["gate_name"] == "transition_readiness"
    assert any(
        update.update.get("gate_source") == "hook"
        and update.update.get("event") == "before_complete_passed"
        for update in updates
    )


@pytest.mark.asyncio
async def test_issue_worker_native_readiness_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "completion": {"require_pushed_head": True},
                    "hooks": {"before_complete_max_feedback_loops": 1},
                },
            },
        )
    )

    class Session:
        async def stop(self) -> None:
            return None

    class FakeRuntime:
        def __init__(self) -> None:
            self.calls = 0

        async def run_turn(
            self,
            session: Any,
            prompt: str,
            issue: Any,
            *,
            on_message=None,
            output_schema=None,
        ) -> StructuredTurnResult:
            self.calls += 1
            return StructuredTurnResult(
                decision="transition",
                summary=f"pass {self.calls}",
                next_state="Done",
            )

    async def fake_native(*_args: Any, **_kwargs: Any) -> HookCommandResult | None:
        return HookCommandResult(status=2, stdout="", stderr="still not pushed")

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.native_readiness_result",
        fake_native,
    )

    tracker = MemoryTracker([make_issue(id="issue-1", identifier="ENG-1")])
    worker = IssueWorker(
        issue=make_issue(id="issue-1", identifier="ENG-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=tracker,
    )
    worker.workspace_path = str(tmp_path / "workspace")
    worker._agent_runtime = FakeRuntime()  # type: ignore[assignment]

    await worker._run_state(Session())
    updated_issue = await tracker.fetch_issue_states_by_ids(["issue-1"])
    assert updated_issue[0].state == snapshot.settings.failure_state


@pytest.mark.asyncio
async def test_native_readiness_result_edge_paths(
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

    async def repo_ok(_workspace: str) -> None:
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.ensure_git_repository", repo_ok
    )

    async def repo_missing(_workspace: str) -> None:
        raise RuntimeError("missing")

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.ensure_git_repository", repo_missing
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "not a git repository" in result.stderr
    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.ensure_git_repository", repo_ok
    )

    async def branch_none(_workspace: str) -> str | None:
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.current_branch_name", branch_none
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "detached" in result.stderr

    async def wrong_branch(_workspace: str) -> str | None:
        return "codex/other"

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.current_branch_name", wrong_branch
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "does not match tracker branch" in result.stderr

    async def correct_branch(_workspace: str) -> str | None:
        return "codex/eng-1"

    async def dirty_status(_workspace: str) -> str:
        return " M file.py"

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.current_branch_name", correct_branch
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.worktree_status", dirty_status
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "worktree is dirty" in result.stderr

    async def clean_status(_workspace: str) -> str:
        return ""

    async def no_upstream(_workspace: str) -> str | None:
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.worktree_status", clean_status
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.upstream_name", no_upstream
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "has no upstream" in result.stderr

    async def with_upstream(_workspace: str) -> str | None:
        return "origin/codex/eng-1"

    async def mismatched_head(_workspace: str, ref: str = "HEAD") -> str:
        return "local" if ref == "HEAD" else "remote"

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.upstream_name", with_upstream
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.head_sha", mismatched_head
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "not fully pushed" in result.stderr

    async def matched_head(_workspace: str, ref: str = "HEAD") -> str:
        return "same"

    async def gh_auth_fail(*_args: Any, **_kwargs: Any) -> None:
        raise ReviewError("gh auth failed")

    monkeypatch.setattr("code_factory.runtime.worker.readiness.head_sha", matched_head)
    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.ensure_github_ready", gh_auth_fail
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "gh auth failed" in result.stderr

    async def gh_auth_ok(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_pr(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        raise ReviewError("No open PR found for branch codex/eng-1.")

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.ensure_github_ready", gh_auth_ok
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.fetch_pull_request", no_pr
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "No open PR found" in result.stderr

    async def stale_pr(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        return 1, "https://example/pr/1", "different"

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.fetch_pull_request", stale_pr
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert "PR head does not match" in result.stderr

    async def matching_pr(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        return 1, "https://example/pr/1", "same"

    monkeypatch.setattr(
        "code_factory.runtime.worker.readiness.fetch_pull_request", matching_pr
    )
    result = await native_readiness_result(str(tmp_path), issue, profile)
    assert result is not None
    assert result.status == 0
    assert "https://example/pr/1" in result.stdout

    pushed_snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "PUSHED_HEAD_ONLY.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "completion": {"require_pushed_head": True},
                },
            },
        )
    )
    pushed_profile = pushed_snapshot.state_profile("In Progress")
    assert pushed_profile is not None
    result = await native_readiness_result(str(tmp_path), issue, pushed_profile)
    assert result is not None
    assert result.status == 0
    assert result.stdout == "same"


@pytest.mark.asyncio
async def test_readiness_capture_uses_review_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, dict[str, str] | None]] = []

    async def fake_capture_shell(
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> Any:
        calls.append((command, cwd, env))
        return "ok"

    monkeypatch.setattr(
        "code_factory.workspace.review_shell.capture_shell", fake_capture_shell
    )
    assert await readiness_module._capture("git status", cwd=str(tmp_path)) == "ok"
    assert calls == [("git status", str(tmp_path), None)]


@pytest.mark.asyncio
async def test_before_complete_helper_edge_paths() -> None:
    prompt = before_complete_feedback_prompt(
        StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        ),
        "x" * 12_001,
        1,
        3,
    )
    assert "truncated: before_complete stderr exceeded 12000 characters" in prompt
    update = before_complete_update(
        "before_complete_passed",
        HookCommandResult(status=0, stdout="ok", stderr=""),
    )
    assert "gate_source" not in update
    assert "gate_name" not in update

    queue: asyncio.Queue[Any] = asyncio.Queue()
    await emit_before_complete_update(
        queue,
        None,
        "before_complete_blocked",
        HookCommandResult(status=2, stdout="", stderr="fix this"),
    )
    assert queue.empty()


@pytest.mark.asyncio
async def test_orchestrator_dispatch_and_reconciliation_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = make_actor(tmp_path)
    issue = make_issue(id="issue-1", identifier="ENG-1", state="Todo")
    stopped: list[str | None] = []
    created_tasks: list[asyncio.Task[Any]] = []
    real_create_task = asyncio.create_task

    class FakeWorker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.issue = kwargs["issue"]

        async def run(self) -> None:
            return None

        async def stop(self, reason: str | None = None) -> None:
            stopped.append(reason)

    def spawn(coro: Any) -> asyncio.Task[Any]:
        task = real_create_task(coro)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.IssueWorker", FakeWorker
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.asyncio.create_task", spawn
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.reconciliation.asyncio.create_task", spawn
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.recovery.asyncio.create_task", spawn
    )

    actor.tracker = MemoryTracker([issue])
    await actor._dispatch_issue(issue, attempt=2)
    assert "issue-1" in actor.running
    assert actor.running["issue-1"].retry_attempt == 2
    await asyncio.gather(*created_tasks)
    created_tasks.clear()

    actor.running["issue-1"].last_agent_timestamp = datetime.now(UTC) - timedelta(
        hours=1
    )
    actor.running["issue-1"].worker = FakeWorker(issue=issue)
    await actor._reconcile_stalled_running_issues()
    await asyncio.gather(*created_tasks)
    created_tasks.clear()
    assert "stall" in stopped

    actor.running["issue-1"].stopping = False
    await actor._reconcile_issue_state(
        make_issue(id="issue-1", identifier="ENG-1", state="Done")
    )
    await asyncio.sleep(0)
    assert actor.running["issue-1"].cleanup_workspace is True

    actor.running["issue-1"].stopping = True
    actor.running["issue-1"].workspace_path = str(tmp_path / "workspaces" / "ENG-1")
    Path(actor.running["issue-1"].workspace_path).mkdir(parents=True, exist_ok=True)
    await actor._handle_worker_exited(
        WorkerExited("issue-1", "ENG-1", actor.running["issue-1"].workspace_path, True)
    )
    await asyncio.gather(*created_tasks)
    created_tasks.clear()
    cleanup_message = await actor.queue.get()
    assert isinstance(cleanup_message, WorkerCleanupComplete)

    actor._handle_worker_cleanup_complete(cleanup_message)
    assert "issue-1" not in actor.running

    entry = RunningEntry(
        issue_id="issue-2",
        identifier="ENG-2",
        issue=make_issue(id="issue-2", identifier="ENG-2", state="In Progress"),
        workspace_path="/tmp/workspaces/ENG-2",
        worker=FakeWorker(issue=issue),
        started_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    actor.running["issue-2"] = entry
    actor.claimed.add("issue-2")
    actor._integrate_agent_update(
        "issue-2",
        {
            "event": "session_started",
            "timestamp": datetime.now(UTC),
            "session_id": "thread-2",
            "runtime_pid": "222",
            "token_usage": {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5},
            "rate_limits": {"primary": {}},
        },
    )
    assert actor.running["issue-2"].turn_count == 1
    assert actor.agent_rate_limits == {"primary": {}}
    assert actor.agent_totals["total_tokens"] == 5
    actor._record_session_completion_totals(entry)
    assert actor.agent_totals["seconds_running"] >= 0

    actor._schedule_issue_retry(
        "issue-2", None, identifier="ENG-2", state_name="In Progress"
    )
    assert "issue-2" in actor.retry_entries
    actor._release_issue_claim("issue-2")
    assert "issue-2" not in actor.retry_entries


@pytest.mark.asyncio
async def test_orchestrator_auto_dispatch_and_reconciliation_edge_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = make_actor(tmp_path)
    actor.tracker = MemoryTracker(
        [make_issue(id="issue-1", identifier="ENG-1", state="Todo")]
    )
    await actor._dispatch_auto_issue(make_issue(id=None, state="Todo"))
    await actor._dispatch_auto_issue(make_issue(id="issue-2", state="Review"))

    class FailingTracker(MemoryTracker):
        async def update_issue_state(self, issue_id: str, state_name: str) -> None:
            raise RuntimeError("boom")

    failing_dir = tmp_path / "failing"
    failing_dir.mkdir()
    failing_actor = make_actor(failing_dir)
    failing_actor.tracker = FailingTracker(
        [make_issue(id="issue-1", identifier="ENG-1", state="Todo")]
    )
    await failing_actor._dispatch_auto_issue(
        make_issue(id="issue-1", identifier="ENG-1", state="Todo")
    )
    assert "issue-1" in failing_actor.retry_entries
    assert failing_actor.retry_entries["issue-1"].state_name == "Todo"

    class MissingRefreshTracker(MemoryTracker):
        async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Any]:
            return []

    missing_refresh_dir = tmp_path / "missing-refresh"
    missing_refresh_dir.mkdir()
    missing_refresh_actor = make_actor(missing_refresh_dir)
    missing_refresh_actor.tracker = MissingRefreshTracker(
        [make_issue(id="issue-1", identifier="ENG-1", state="Todo")]
    )
    await missing_refresh_actor._dispatch_auto_issue(
        make_issue(id="issue-1", identifier="ENG-1", state="Todo")
    )
    assert "issue-1" not in missing_refresh_actor.claimed

    blocked_dir = tmp_path / "blocked"
    blocked_dir.mkdir()
    blocked_actor = make_actor(blocked_dir)
    blocked_actor.tracker = MemoryTracker(
        [make_issue(id="issue-1", identifier="ENG-1", state="Todo")]
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.available_slots",
        lambda settings, running: 0,
    )
    await blocked_actor._dispatch_auto_issue(
        make_issue(id="issue-1", identifier="ENG-1", state="Todo")
    )
    assert (
        blocked_actor.retry_entries["issue-1"].error
        == "no available orchestrator slots"
    )
    assert blocked_actor.retry_entries["issue-1"].mode == "wait"

    release_dir = tmp_path / "release"
    release_dir.mkdir()
    release_actor = make_actor(release_dir)
    release_actor.tracker = MemoryTracker(
        [make_issue(id="issue-1", identifier="ENG-1", state="Todo")]
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.available_slots",
        lambda settings, running: 1,
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.candidate_issue",
        lambda settings, issue: False,
    )
    await release_actor._dispatch_auto_issue(
        make_issue(id="issue-1", identifier="ENG-1", state="Todo")
    )
    assert "issue-1" not in release_actor.claimed

    entry = RunningEntry(
        issue_id="issue-3",
        identifier="ENG-3",
        issue=make_issue(id="issue-3", identifier="ENG-3", state="In Progress"),
        workspace_path="/tmp/workspaces/ENG-3",
        worker=object(),
        started_at=datetime.now(UTC),
    )
    actor.running["issue-3"] = entry
    actor.claimed.add("issue-3")
    await actor._handle_worker_exited(
        WorkerExited(
            issue_id="issue-3",
            identifier="ENG-3",
            workspace_path="/tmp/workspaces/ENG-3",
            normal=True,
            completed=False,
        )
    )
    assert (
        actor.retry_entries["issue-3"].error
        == "worker exited without completing a state transition"
    )

    exhausted_dir = tmp_path / "exhausted"
    exhausted_dir.mkdir()
    exhausted_actor = make_actor(
        exhausted_dir, workflow_overrides={"agent": {"max_worker_retries": 1}}
    )
    exhausted_actor.tracker = MemoryTracker(
        [make_issue(id="issue-9", identifier="ENG-9", state="In Progress")]
    )
    exhausted_entry = RunningEntry(
        issue_id="issue-9",
        identifier="ENG-9",
        issue=make_issue(id="issue-9", identifier="ENG-9", state="In Progress"),
        workspace_path=str(exhausted_dir / "workspaces" / "ENG-9"),
        worker=object(),
        started_at=datetime.now(UTC),
        retry_attempt=1,
    )
    exhausted_actor.running["issue-9"] = exhausted_entry
    exhausted_actor.claimed.add("issue-9")
    await exhausted_actor._handle_worker_exited(
        WorkerExited(
            issue_id="issue-9",
            identifier="ENG-9",
            workspace_path=exhausted_entry.workspace_path,
            normal=False,
            reason="boom",
        )
    )
    refreshed = await exhausted_actor.tracker.fetch_issue_states_by_ids(["issue-9"])
    assert refreshed[0].state == exhausted_actor.settings.failure_state
    assert "issue-9" not in exhausted_actor.retry_entries


@pytest.mark.asyncio
async def test_orchestrator_message_and_refresh_paths(tmp_path: Path) -> None:
    actor = make_actor(tmp_path)
    actor.next_poll_due_at_ms = actor._monotonic_ms() + 1_000

    refresh = actor._queue_refresh()
    assert refresh["queued"] is True
    assert refresh["coalesced"] is False
    second = actor._queue_refresh()
    assert second["coalesced"] is True

    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    await actor._handle_message(SnapshotRequest(future))
    assert (
        future.result()["polling"]["poll_interval_ms"]
        == actor.settings.polling.interval_ms
    )

    future = asyncio.get_running_loop().create_future()
    await actor._handle_message(RefreshRequest(future))
    assert future.result()["queued"] is True

    future = asyncio.get_running_loop().create_future()
    await actor._handle_message(Shutdown(future))
    assert future.result() is True
    assert actor._shutdown is True

    snapshot = make_snapshot(write_workflow_file(tmp_path / "NEW_WORKFLOW.md"))
    await actor._handle_message(WorkflowUpdated(snapshot))
    assert actor.workflow_snapshot.path == str(tmp_path / "NEW_WORKFLOW.md")

    await actor._handle_message(WorkflowReloadError("boom"))
    assert actor.workflow_reload_error == "'boom'"


@pytest.mark.asyncio
async def test_orchestrator_refresh_applies_reloaded_snapshot_immediately(
    tmp_path: Path,
) -> None:
    original = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    updated = replace(
        make_snapshot(
            write_workflow_file(
                tmp_path / "UPDATED_WORKFLOW.md",
                agent={"max_concurrent_agents": 2},
            )
        ),
        version=2,
    )

    async def fake_reload() -> Any:
        return updated

    actor = OrchestratorActor(
        original,
        tracker_factory=lambda settings: MemoryTracker([]),
        reload_workflow_if_changed=fake_reload,
    )
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    await actor._handle_message(RefreshRequest(future))
    assert future.result()["queued"] is True
    assert actor.workflow_snapshot.version == 2
    assert actor.settings.agent.max_concurrent_agents == 2


@pytest.mark.asyncio
async def test_orchestrator_replace_workflow_ignores_identical_snapshot(
    tmp_path: Path,
) -> None:
    actor = make_actor(tmp_path)
    current = actor.workflow_snapshot
    original_tracker = actor.tracker
    await actor._replace_workflow(current)
    assert actor.workflow_snapshot is current
    assert actor.tracker is original_tracker
