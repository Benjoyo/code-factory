from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from code_factory.coding_agents.codex.app_server.streams import log_non_json_stream_line
from code_factory.config.utils import (
    coerce_int,
    normalize_state_limits,
    resolve_env_value,
    string_list,
)
from code_factory.errors import (
    ConfigValidationError,
    TrackerClientError,
    WorkspaceError,
)
from code_factory.observability.api.payloads import (
    due_at_iso8601,
    humanize_agent_message,
    iso8601,
    recent_events_payload,
)
from code_factory.observability.api.server import ObservabilityHTTPServer
from code_factory.runtime.messages import Shutdown
from code_factory.runtime.orchestration.actor import OrchestratorActor
from code_factory.runtime.orchestration.models import RetryEntry, RunningEntry
from code_factory.runtime.subprocess.process_tree import ProcessTree
from code_factory.runtime.worker.actor import IssueWorker
from code_factory.trackers.linear.client import (
    LinearClient,
)
from code_factory.trackers.linear.client import (
    build_tracker as build_linear_tracker,
)
from code_factory.trackers.linear.config import (
    validate_tracker_settings as validate_linear_tracker_settings,
)
from code_factory.trackers.linear.decoding import (
    decode_linear_page_response,
    decode_linear_response,
    extract_blockers,
    next_page_cursor,
)
from code_factory.trackers.linear.graphql import LinearGraphQLClient
from code_factory.trackers.linear.queries import (
    STATE_LOOKUP_QUERY,
    UPDATE_STATE_MUTATION,
)
from code_factory.workflow.store import WorkflowStoreActor
from code_factory.workspace.hooks import run_hook
from code_factory.workspace.manager import WorkspaceManager
from code_factory.workspace.paths import validate_workspace_path

from .conftest import make_issue, make_snapshot, write_workflow_file


def make_settings(tmp_path: Path, *, overrides: dict[str, Any] | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", **(overrides or {}))
    return make_snapshot(workflow).settings


def make_actor(
    tmp_path: Path, *, workflow_overrides: dict[str, Any] | None = None
) -> OrchestratorActor:
    tmp_path.mkdir(parents=True, exist_ok=True)
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md", **(workflow_overrides or {})
    )
    snapshot = make_snapshot(workflow)
    return OrchestratorActor(
        snapshot,
        tracker_factory=lambda settings: cast(Any, object()),
    )


@pytest.mark.asyncio
async def test_observability_workspace_and_hook_edge_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert recent_events_payload({"last_agent_timestamp": None}) == []
    assert humanize_agent_message({"message": {"nested": True}}) == {"nested": True}
    assert humanize_agent_message("raw") == "raw"
    assert iso8601("not-a-datetime") is None
    assert due_at_iso8601("soon") is None

    settings = make_settings(tmp_path, overrides={"hooks": {"before_remove": "rm -rf"}})
    manager = WorkspaceManager(settings)
    manager.validate_workspace_path(manager.workspace_path_for_issue("ENG-0"))
    workspace_file = Path(manager.workspace_path_for_issue("ENG-1"))
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("file\n", encoding="utf-8")
    assert await manager.remove(str(workspace_file)) == []
    assert workspace_file.exists() is False
    assert await manager.remove(str(workspace_file)) == []

    await manager.remove_issue_workspaces(None)

    async def failing_remove(_workspace: str) -> list[str]:
        raise WorkspaceError("boom")

    manager.remove = failing_remove  # type: ignore[method-assign]
    await manager.remove_issue_workspaces("ENG-2")

    async def fail_run_hook(*_args: Any, **_kwargs: Any) -> None:
        raise WorkspaceError("boom")

    with caplog.at_level(logging.WARNING):
        monkeypatch.setattr("code_factory.workspace.manager.run_hook", fail_run_hook)
        await manager._run_before_remove_hook("/tmp/workspace")
    assert any(
        "Ignoring before_remove hook failure" in record.message
        for record in caplog.records
    )

    class FailingHookProcess:
        async def capture_output(self, _timeout_ms: int) -> tuple[int, str]:
            return 1, "hook failed"

        async def terminate(self) -> None:
            return None

    async def spawn_failing_process(*_args: Any, **_kwargs: Any) -> FailingHookProcess:
        return FailingHookProcess()

    monkeypatch.setattr(
        "code_factory.workspace.hooks.ProcessTree.spawn_shell",
        spawn_failing_process,
    )
    with pytest.raises(WorkspaceError, match="workspace_hook_failed"):
        await run_hook(
            settings,
            "echo nope",
            "/tmp/workspace",
            {"issue_id": "issue-1", "issue_identifier": "ENG-1"},
            "after_run",
            fatal=False,
        )
    with pytest.raises(WorkspaceError, match="workspace_hook_failed"):
        await run_hook(
            settings,
            "echo nope",
            "/tmp/workspace",
            {"issue_id": "issue-1", "issue_identifier": "ENG-1"},
            "before_run",
            fatal=True,
        )

    monkeypatch.setattr(
        "code_factory.workspace.paths.canonicalize",
        lambda _path: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(WorkspaceError, match="workspace_path_unreadable"):
        validate_workspace_path("/tmp/root", "/tmp/workspace")


@pytest.mark.asyncio
async def test_linear_client_and_decoding_edge_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    validate_linear_tracker_settings(settings)

    class BuildTrackerGraphQL:
        async def close(self) -> None:
            return None

        async def request(
            self,
            query: str,
            variables: dict[str, Any] | None = None,
            operation_name: str | None = None,
        ) -> dict[str, Any]:
            return {"data": {"ok": True}}

    assert isinstance(
        build_linear_tracker(
            settings, client_factory=cast(Any, lambda: BuildTrackerGraphQL())
        ),
        LinearClient,
    )

    class UpdateGraphQL:
        async def close(self) -> None:
            return None

        async def request(
            self,
            query: str,
            variables: dict[str, Any] | None = None,
            operation_name: str | None = None,
        ) -> dict[str, Any]:
            if query == UPDATE_STATE_MUTATION:
                return {"data": {"issueUpdate": {"success": False}}}
            if query == STATE_LOOKUP_QUERY:
                return {
                    "data": {
                        "issue": {"team": {"states": {"nodes": [{"id": "state-1"}]}}}
                    }
                }
            return {"data": {"ok": True}}

    client = LinearClient(settings, client_factory=cast(Any, lambda: UpdateGraphQL()))
    assert await client.fetch_issues_by_states([]) == []
    assert await client.fetch_issue_states_by_ids([]) == []
    assert await client._routing_assignee_filter() is None
    assert await client.graphql("query Ping { ping }", {"ok": True}) == {
        "data": {"ok": True}
    }
    with pytest.raises(TrackerClientError, match="issue_update_failed"):
        await client.update_issue_state("issue-1", "Done")

    class MissingStateGraphQL:
        async def close(self) -> None:
            return None

        async def request(
            self,
            query: str,
            variables: dict[str, Any] | None = None,
            operation_name: str | None = None,
        ) -> dict[str, Any]:
            assert query == STATE_LOOKUP_QUERY
            return {"data": {"issue": {"team": {"states": {"nodes": [{}]}}}}}

    missing_state_client = LinearClient(
        settings, client_factory=cast(Any, lambda: MissingStateGraphQL())
    )
    with pytest.raises(TrackerClientError, match="state_not_found"):
        await missing_state_client._resolve_state_id("issue-1", "Done")

    with pytest.raises(TrackerClientError, match="linear_graphql_errors"):
        decode_linear_page_response({"errors": ["boom"]}, None)
    with pytest.raises(TrackerClientError, match="linear_graphql_errors"):
        decode_linear_response({"errors": ["boom"]}, None)
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        decode_linear_page_response({"data": {"issues": "bad"}}, None)
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        decode_linear_page_response({"data": {"issues": {"nodes": []}}}, None)
    with pytest.raises(TrackerClientError, match="linear_missing_end_cursor"):
        next_page_cursor({"has_next_page": True, "end_cursor": ""})
    assert (
        extract_blockers(
            {"inverseRelations": {"nodes": [{"type": "blocks", "issue": "bad"}]}}
        )
        == []
    )


@pytest.mark.asyncio
async def test_config_utils_linear_graphql_client_and_worker_edge_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ConfigValidationError, match="field must be an integer"):
        coerce_int([], "field")
    with pytest.raises(ConfigValidationError, match="field must be a list of strings"):
        string_list("bad", "field", ())
    with pytest.raises(ConfigValidationError, match="field must be an object"):
        normalize_state_limits([], "field")
    monkeypatch.setenv("SECRET_TOKEN", "value")
    assert resolve_env_value("$SECRET_TOKEN", "fallback") == "value"

    settings = make_settings(tmp_path / "graphql")

    class FakeAsyncClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

        async def post(
            self, url: str, *, headers: dict[str, str], json: dict[str, Any]
        ) -> httpx.Response:
            self.calls.append((url, headers, json))
            return httpx.Response(200, json={"data": {"ok": True}})

        async def aclose(self) -> None:
            return None

    http_client = FakeAsyncClient()
    gql_client = LinearGraphQLClient(settings, client=cast(Any, http_client))
    assert await gql_client.request("query Ping { ping }", {"ok": True}) == {
        "data": {"ok": True}
    }
    assert http_client.calls[0][0] == settings.tracker.endpoint

    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(parents=True, exist_ok=True)
    workflow = write_workflow_file(worker_dir / "WORKFLOW.md")
    snapshot = make_snapshot(workflow)
    queue: asyncio.Queue[Any] = asyncio.Queue()

    class StopEarlyManager:
        async def create_for_issue(self, issue: Any) -> Any:
            worker.stop_event.set()
            return type("Workspace", (), {"path": str(tmp_path / "workspace")})()

        async def run_before_run_hook(self, workspace: str, issue: Any) -> None:
            return None

        async def run_after_run_hook(self, workspace: str, issue: Any) -> None:
            return None

    class FakeRuntime:
        async def start_session(self, workspace: str) -> Any:
            raise AssertionError("start_session should not run")

        async def run_turn(
            self,
            session: Any,
            prompt: str,
            issue: Any,
            *,
            on_message=None,
            output_schema=None,
        ) -> dict[str, Any]:
            raise AssertionError("run_turn should not run")

    worker = IssueWorker(
        issue=make_issue(id="issue-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=queue,
    )
    worker.workspace_manager = StopEarlyManager()  # type: ignore[assignment]
    worker._agent_runtime = FakeRuntime()  # type: ignore[assignment]
    await worker.run()
    exited = await queue.get()
    assert exited.normal is True
    assert exited.reason == "stopped"

    paused_worker = IssueWorker(
        issue=make_issue(id="issue-2"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
    )
    paused_worker.stop_event.set()
    paused_worker._agent_runtime = FakeRuntime()  # type: ignore[assignment]
    await paused_worker._run_state(cast(Any, object()))

    no_id_worker = IssueWorker(
        issue=make_issue(id=None),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
    )
    await no_id_worker._on_agent_message({"event": "notification"})
    assert no_id_worker.queue.empty()

    class FailingCreateManager:
        async def create_for_issue(self, issue: Any) -> Any:
            raise RuntimeError("boom")

        async def run_before_run_hook(self, workspace: str, issue: Any) -> None:
            return None

        async def run_after_run_hook(self, workspace: str, issue: Any) -> None:
            raise AssertionError("after_run should not execute without a workspace")

    failed_worker_queue: asyncio.Queue[Any] = asyncio.Queue()
    failed_worker = IssueWorker(
        issue=make_issue(id="issue-3"),
        workflow_snapshot=snapshot,
        orchestrator_queue=failed_worker_queue,
    )
    failed_worker.workspace_manager = FailingCreateManager()  # type: ignore[assignment]
    failed_worker._agent_runtime = FakeRuntime()  # type: ignore[assignment]
    with caplog.at_level(logging.ERROR):
        await failed_worker.run()
    failed_exit = await failed_worker_queue.get()
    assert failed_exit.normal is False
    assert "boom" in (failed_exit.reason or "")
    assert "Issue worker failed" in caplog.text

    class StoppedFailingManager:
        async def create_for_issue(self, issue: Any) -> Any:
            stopped_failed_worker.stop_event.set()
            raise RuntimeError("ignored")

        async def run_before_run_hook(self, workspace: str, issue: Any) -> None:
            return None

        async def run_after_run_hook(self, workspace: str, issue: Any) -> None:
            raise AssertionError("after_run should not execute without a workspace")

    stopped_failed_queue: asyncio.Queue[Any] = asyncio.Queue()
    stopped_failed_worker = IssueWorker(
        issue=make_issue(id="issue-4"),
        workflow_snapshot=snapshot,
        orchestrator_queue=stopped_failed_queue,
    )
    stopped_failed_worker.workspace_manager = StoppedFailingManager()  # type: ignore[assignment]
    stopped_failed_worker._agent_runtime = FakeRuntime()  # type: ignore[assignment]
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        await stopped_failed_worker.run()
    stopped_failed_exit = await stopped_failed_queue.get()
    assert stopped_failed_exit.normal is True
    assert stopped_failed_exit.reason == "stopped"
    assert "Issue worker failed" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        log_non_json_stream_line("   ", "stderr")
    assert not caplog.records


@pytest.mark.asyncio
async def test_server_store_and_actor_pragmatic_exit_paths(tmp_path: Path) -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    server = ObservabilityHTTPServer(cast(Any, object()), host="127.0.0.1", port=1)
    await server.run(stop_event)

    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    store = WorkflowStoreActor(
        str(workflow),
        on_snapshot=lambda snapshot: asyncio.sleep(0),
        on_error=None,
    )
    await store.load_initial_snapshot()
    await store._handle_reload_error(RuntimeError("boom"))

    actor = make_actor(tmp_path / "actor-shutdown")
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    await actor._handle_message(Shutdown(future))
    assert actor._shutdown is True
    assert future.result() is True


@pytest.mark.asyncio
async def test_process_tree_and_orchestration_edge_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 321
            self.returncode = None

        async def wait(self) -> int:
            return 0

    signals: list[tuple[int, int]] = []
    wait_calls = {"count": 0}

    async def fake_wait_for(awaitable: Any, _timeout: float) -> Any:
        wait_calls["count"] += 1
        if wait_calls["count"] == 1:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise TimeoutError
        return await awaitable

    monkeypatch.setattr(
        "code_factory.runtime.subprocess.process_tree.asyncio.wait_for",
        fake_wait_for,
    )
    monkeypatch.setattr(
        "code_factory.runtime.subprocess.process_tree.os.killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    tree = ProcessTree(process=cast(Any, FakeProcess()), command="cmd", cwd="/tmp")
    await tree.terminate()
    assert signals == [(321, signal.SIGTERM), (321, signal.SIGKILL)]

    actor = make_actor(tmp_path / "actor")

    class FailingStartupTracker:
        async def fetch_issues_by_states(self, _states: list[str]) -> list[Any]:
            raise RuntimeError("fetch failed")

    actor.tracker = cast(Any, FailingStartupTracker())
    with caplog.at_level(logging.WARNING):
        await actor.startup_terminal_workspace_cleanup()
    assert any(
        "Skipping startup terminal workspace cleanup" in record.message
        for record in caplog.records
    )

    cleaned: list[str] = []

    class CleanupTracker:
        async def fetch_issues_by_states(self, _states: list[str]) -> list[Any]:
            return [make_issue(identifier=None), make_issue(identifier="ENG-1")]

    class FailingManager:
        def __init__(self, _settings: Any) -> None:
            return None

        async def remove_issue_workspaces(self, identifier: str | None) -> None:
            cleaned.append(identifier or "")
            raise RuntimeError("ignore me")

    actor.tracker = cast(Any, CleanupTracker())
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.actor.WorkspaceManager",
        FailingManager,
    )
    await actor.startup_terminal_workspace_cleanup()
    assert cleaned == ["ENG-1"]

    revalidate_actor = make_actor(tmp_path / "dispatch")
    issue = make_issue(id=None)
    assert await revalidate_actor._revalidate_issue_for_dispatch(issue) == issue

    class FailingTracker:
        async def fetch_issue_states_by_ids(self, _ids: list[str]) -> list[Any]:
            raise RuntimeError("refresh failed")

    revalidate_actor.tracker = cast(Any, FailingTracker())
    assert await revalidate_actor._revalidate_issue_for_dispatch(make_issue()) is None

    class EmptyTracker:
        async def fetch_issue_states_by_ids(self, _ids: list[str]) -> list[Any]:
            return []

    revalidate_actor.tracker = cast(Any, EmptyTracker())
    assert await revalidate_actor._revalidate_issue_for_dispatch(make_issue()) is None

    stale_entry = RetryEntry(
        issue_id="issue-1",
        identifier="ENG-1",
        attempt=1,
        due_at_ms=0,
        token="stale",
    )

    class StaleRetryEntries(dict[str, RetryEntry]):
        def values(self):  # type: ignore[override]
            return [stale_entry]

        def get(self, key: str, default: Any = None):  # type: ignore[override]
            return RetryEntry(
                issue_id=key,
                identifier="ENG-1",
                attempt=1,
                due_at_ms=0,
                token="current",
            )

    revalidate_actor.retry_entries = cast(Any, StaleRetryEntries())
    await revalidate_actor._run_due_retries(0)
    await revalidate_actor._reconcile_issue_state(
        make_issue(id="missing-active", state="In Progress")
    )

    class RetryTracker:
        async def fetch_candidate_issues(self) -> list[Any]:
            return []

        async def fetch_issue_states_by_ids(self, _ids: list[str]) -> list[Any]:
            raise RuntimeError("gone")

    revalidate_actor.tracker = cast(Any, RetryTracker())
    revalidate_actor.claimed.add("issue-2")
    await revalidate_actor._handle_retry_entry(
        RetryEntry(
            issue_id="issue-2",
            identifier="ENG-2",
            attempt=1,
            due_at_ms=0,
            token="token",
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    assert "issue-2" not in revalidate_actor.claimed

    class NonCandidateRetryTracker:
        async def fetch_candidate_issues(self) -> list[Any]:
            return []

        async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Any]:
            return [make_issue(id=issue_ids[0], state="Blocked")]

    revalidate_actor.tracker = cast(Any, NonCandidateRetryTracker())
    revalidate_actor.claimed.add("issue-5")
    await revalidate_actor._handle_retry_entry(
        RetryEntry(
            issue_id="issue-5",
            identifier="ENG-5",
            attempt=1,
            due_at_ms=0,
            token="token",
            error="old",
        )
    )
    assert "issue-5" not in revalidate_actor.claimed

    class CleanupManager:
        async def remove(self, _workspace: str) -> None:
            raise RuntimeError("cleanup failed")

        async def remove_issue_workspaces(self, _identifier: str) -> None:
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(
        revalidate_actor,
        "_workspace_manager_for_path",
        lambda _workspace: CleanupManager(),
    )
    await revalidate_actor._cleanup_retry_issue_workspace(
        make_issue(identifier="ENG-3"),
        str(tmp_path / "workspace"),
    )
    await revalidate_actor._cleanup_retry_issue_workspace(
        make_issue(identifier=None),
        None,
    )

    running_entry = RunningEntry(
        issue_id="issue-3",
        identifier="ENG-3",
        issue=make_issue(id="issue-3"),
        workspace_path="/tmp/workspace",
        worker=cast(Any, object()),
        started_at=datetime.now(UTC),
        stopping=True,
    )
    revalidate_actor.running["issue-3"] = running_entry
    await revalidate_actor._reconcile_stalled_running_issues()
    assert revalidate_actor.running["issue-3"].stopping is True

    revalidate_actor.claimed.add("missing")
    await revalidate_actor._terminate_running_issue(
        "missing", cleanup_workspace=False, reason="missing"
    )
    assert "missing" not in revalidate_actor.claimed

    class StoppingWorker:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        async def stop(self, reason: str | None = None) -> None:
            self.calls.append(reason)
            raise RuntimeError("ignore during shutdown")

    shutdown_worker = StoppingWorker()
    revalidate_actor.running["issue-4"] = RunningEntry(
        issue_id="issue-4",
        identifier="ENG-4",
        issue=make_issue(id="issue-4"),
        workspace_path="/tmp/workspace",
        worker=shutdown_worker,
        started_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    await revalidate_actor._shutdown_runtime()
    assert shutdown_worker.calls == ["shutdown"]
    empty_actor = make_actor(tmp_path / "deadlines")
    empty_actor.next_poll_due_at_ms = None
    empty_actor.poll_run_due_at_ms = None
    assert empty_actor._next_timeout_seconds() is None
