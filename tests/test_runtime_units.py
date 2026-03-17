from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from symphony.runtime.messages import (
    AgentWorkerUpdate,
    RefreshRequest,
    Shutdown,
    SnapshotRequest,
    WorkerCleanupComplete,
    WorkerExited,
    WorkflowReloadError,
    WorkflowUpdated,
)
from symphony.runtime.orchestration.actor import OrchestratorActor
from symphony.runtime.orchestration.models import RetryEntry, RunningEntry
from symphony.runtime.orchestration.policy import (
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
from symphony.runtime.orchestration.snapshot import snapshot_payload
from symphony.runtime.orchestration.tokens import (
    apply_token_delta,
    compute_token_delta,
    extract_rate_limits,
    extract_token_delta,
    extract_token_usage,
    get_token_usage,
    integer_like,
)
from symphony.runtime.subprocess.process_tree import ProcessTree
from symphony.runtime.worker.actor import IssueWorker
from symphony.runtime.worker.utils import tracker_state_is_active
from symphony.trackers.memory import MemoryTracker
from symphony.workspace.manager import WorkspaceManager

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
        agent_totals={"total_tokens": 3},
        rate_limits={"primary": {}},
        poll_check_in_progress=True,
        next_poll_due_at_ms=500,
        poll_interval_ms=200,
        now_ms=100,
    )
    assert payload["running"][0]["issue_id"] == "issue-1"
    assert payload["retrying"][0]["attempt"] == 2
    assert payload["polling"]["next_poll_in_ms"] == 400
    assert actor.snapshot_now()["polling"]["poll_interval_ms"] == 30_000


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
    monkeypatch.setattr("symphony.runtime.subprocess.process_tree.os.name", "nt")
    await tree.terminate()
    assert terminating.terminated is True


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
            self, session: Any, prompt: str, issue: Any, *, on_message=None
        ) -> dict[str, Any]:
            self.prompts.append(prompt)
            if on_message is not None:
                await on_message(
                    {"event": "notification", "timestamp": datetime.now(UTC)}
                )
            return {"result": "turn_completed"}

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
        [make_issue(id="issue-1", identifier="ENG-1", state="Done")]
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
    assert worker._turn_prompt(issue, 2, 3).startswith("Continuation guidance")
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
        "symphony.runtime.orchestration.dispatching.IssueWorker", FakeWorker
    )
    monkeypatch.setattr(
        "symphony.runtime.orchestration.dispatching.asyncio.create_task", spawn
    )
    monkeypatch.setattr(
        "symphony.runtime.orchestration.reconciliation.asyncio.create_task", spawn
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
    await asyncio.sleep(0)
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
    await asyncio.sleep(0)
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

    actor._schedule_issue_retry("issue-2", None, identifier="ENG-2")
    assert "issue-2" in actor.retry_entries
    actor._release_issue_claim("issue-2")
    assert "issue-2" not in actor.retry_entries


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
