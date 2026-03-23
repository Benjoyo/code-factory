from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from code_factory.coding_agents.codex.app_server.client import AppServerClient
from code_factory.coding_agents.codex.app_server.protocol import steer_turn
from code_factory.coding_agents.codex.app_server.routing import route_stdout
from code_factory.coding_agents.codex.runtime import CodexRuntime
from code_factory.errors import AppServerError, ControlRequestError
from code_factory.observability.api.client import ControlEndpoint, steer_issue
from code_factory.observability.api.server import ObservabilityHTTPServer
from code_factory.observability.runtime_metadata import (
    clear_runtime_metadata,
    read_runtime_metadata,
    runtime_metadata_path,
)
from code_factory.runtime.orchestration import OrchestratorActor
from code_factory.runtime.orchestration.models import RetryEntry, RunningEntry
from code_factory.runtime.worker.actor import IssueWorker
from code_factory.trackers.memory import MemoryTracker

from .conftest import make_issue, make_snapshot, write_workflow_file
from .test_coding_agents_and_observability import make_session, make_settings


@pytest.mark.asyncio
async def test_route_stdout_routes_pending_responses_and_events() -> None:
    raw_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    pending = {7: future}
    task = asyncio.create_task(route_stdout(raw_queue, event_queue, pending))
    await raw_queue.put(("stderr", "ignored"))
    assert await event_queue.get() == ("stderr", "ignored")
    await raw_queue.put(("line", json.dumps({"id": 7, "result": {"turnId": "turn-7"}})))
    assert await future == {"turnId": "turn-7"}
    await raw_queue.put(("line", "not-json"))
    assert await event_queue.get() == ("line", "not-json")
    await raw_queue.put(("line", json.dumps({"method": "turn/completed"})))
    assert (await event_queue.get())[0] == "line"
    await raw_queue.put(("exit", 9))
    assert await event_queue.get() == ("exit", 9)
    await task


@pytest.mark.asyncio
async def test_route_stdout_covers_error_and_cancel_paths() -> None:
    raw_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    pending = {7: future}
    task = asyncio.create_task(route_stdout(raw_queue, event_queue, pending))
    await raw_queue.put(("line", json.dumps({"id": 7, "error": {"message": "bad"}})))
    with pytest.raises(AppServerError, match="response_error"):
        await future

    future = asyncio.get_running_loop().create_future()
    pending[8] = future
    await raw_queue.put(("line", json.dumps({"id": 8, "result": []})))
    with pytest.raises(AppServerError, match="response_error"):
        await future

    future = asyncio.get_running_loop().create_future()
    pending[9] = future
    done_future: asyncio.Future[dict[str, Any]] = (
        asyncio.get_running_loop().create_future()
    )
    done_future.set_result({"done": True})
    pending[10] = done_future
    await raw_queue.put(("exit", 5))
    with pytest.raises(AppServerError, match="port_exit"):
        await future
    await task

    raw_queue = asyncio.Queue()
    event_queue = asyncio.Queue()
    future = asyncio.get_running_loop().create_future()
    done_future = asyncio.get_running_loop().create_future()
    done_future.set_result({"done": True})
    pending = {10: future, 11: done_future}
    task = asyncio.create_task(route_stdout(raw_queue, event_queue, pending))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert future.cancelled() is True

    raw_queue = asyncio.Queue()
    event_queue = asyncio.Queue()
    done_future = asyncio.get_running_loop().create_future()
    done_future.set_result({"done": True})
    pending = {12: done_future}
    task = asyncio.create_task(route_stdout(raw_queue, event_queue, pending))
    await raw_queue.put(("line", json.dumps({"id": 12, "result": {"skip": True}})))
    await raw_queue.put(("exit", 0))
    await task


@pytest.mark.asyncio
async def test_steer_turn_validates_active_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = make_session()
    with pytest.raises(AppServerError, match="no_active_turn"):
        await steer_turn(session, "focus")

    session.current_turn_id = "turn-1"

    async def fake_session_request(
        _session: Any, method: str, params: dict[str, Any], *, timeout_ms: int | None
    ) -> dict[str, Any]:
        assert method == "turn/steer"
        assert params["expectedTurnId"] == "turn-1"
        assert params["input"][0]["text"] == "focus"
        assert timeout_ms == session.read_timeout_ms
        return {"turnId": "turn-1"}

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.session_request",
        fake_session_request,
    )
    assert await steer_turn(session, "focus") == "turn-1"

    async def invalid_session_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"turnId": None}

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.session_request",
        invalid_session_request,
    )
    with pytest.raises(AppServerError, match="invalid_turn_steer_payload"):
        await steer_turn(session, "focus")


@pytest.mark.asyncio
async def test_orchestrator_request_steer_handles_success_and_failures(
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    actor = OrchestratorActor(
        snapshot, tracker_factory=lambda settings: MemoryTracker([])
    )
    issue = make_issue(id="issue-1", identifier="ENG-1", state="In Progress")
    steers: list[str] = []

    class FakeWorker:
        async def steer(self, message: str) -> str:
            steers.append(message)
            return "turn-live"

    actor.running["issue-1"] = RunningEntry(
        issue_id="issue-1",
        identifier=issue.identifier,
        issue=issue,
        workspace_path="/tmp/workspaces/ENG-1",
        worker=FakeWorker(),
        started_at=datetime.now(UTC),
        thread_id="thread-live",
        turn_id="turn-live",
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(actor.run(stop_event))
    try:
        result = await actor.request_steer("ENG-1", "focus on tests")
        assert result["thread_id"] == "thread-live"
        assert result["turn_id"] == "turn-live"
        assert steers == ["focus on tests"]

        actor.retry_entries["issue-2"] = RetryEntry(
            "issue-2", "ENG-2", 1, 1_000, "token"
        )
        with pytest.raises(ControlRequestError, match="not currently steerable"):
            await actor.request_steer("ENG-2", "focus")
        actor.running["issue-3"] = RunningEntry(
            issue_id="issue-3",
            identifier="ENG-3",
            issue=issue,
            workspace_path="/tmp/workspaces/ENG-3",
            worker=FakeWorker(),
            started_at=datetime.now(UTC),
            thread_id="thread-live",
            turn_id=None,
            stopping=True,
        )
        with pytest.raises(ControlRequestError, match="not currently steerable"):
            await actor.request_steer("ENG-3", "focus")
        with pytest.raises(ControlRequestError, match="issue not found"):
            await actor.request_steer("ENG-404", "focus")
    finally:
        await actor.shutdown()
        stop_event.set()
        await task


@pytest.mark.asyncio
async def test_orchestrator_request_steer_wraps_worker_failures(tmp_path: Path) -> None:
    snapshot = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    actor = OrchestratorActor(
        snapshot, tracker_factory=lambda settings: MemoryTracker([])
    )
    issue = make_issue(id="issue-1", identifier="ENG-1", state="In Progress")

    class FailingWorker:
        async def steer(self, _message: str) -> str:
            raise RuntimeError("boom")

    actor.running["issue-1"] = RunningEntry(
        issue_id="issue-1",
        identifier=issue.identifier,
        issue=issue,
        workspace_path="/tmp/workspaces/ENG-1",
        worker=FailingWorker(),
        started_at=datetime.now(UTC),
        thread_id="thread-live",
        turn_id="turn-live",
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(actor.run(stop_event))
    try:
        with pytest.raises(ControlRequestError, match="ENG-1: boom"):
            await actor.request_steer("ENG-1", "focus")
    finally:
        await actor.shutdown()
        stop_event.set()
        await task


@pytest.mark.asyncio
async def test_observability_server_steer_endpoint_and_runtime_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = tmp_path / "WORKFLOW.md"

    class FakeOrchestrator:
        async def snapshot(self) -> dict[str, Any]:
            return {
                "running": [],
                "retrying": [],
                "agent_totals": {},
                "rate_limits": {},
            }

        async def request_refresh(self) -> dict[str, Any]:
            return {"queued": True}

        async def request_steer(
            self, issue_identifier: str, message: str
        ) -> dict[str, Any]:
            if issue_identifier == "ENG-409":
                raise ControlRequestError(
                    "issue_not_steerable",
                    "ENG-409: issue is not currently steerable",
                    409,
                )
            return {
                "accepted": True,
                "issue_identifier": issue_identifier,
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "accepted_at": "2026-03-24T00:00:00Z",
            }

    server = ObservabilityHTTPServer(
        cast(Any, FakeOrchestrator()),
        host="127.0.0.1",
        port=4321,
        workflow_path=str(workflow.resolve()),
    )
    request = cast(
        Any,
        SimpleNamespace(
            match_info={"issue_identifier": "ENG-1"},
            can_read_body=True,
            json=lambda: asyncio.sleep(0, result={"message": "focus"}),
        ),
    )
    response = await server.steer(request)
    assert response.status == 202

    bad_request = cast(
        Any,
        SimpleNamespace(
            match_info={"issue_identifier": "ENG-1"},
            can_read_body=True,
            json=lambda: asyncio.sleep(0, result={}),
        ),
    )
    assert (await server.steer(bad_request)).status == 400
    exploding_request = cast(
        Any,
        SimpleNamespace(
            match_info={"issue_identifier": "ENG-1"},
            can_read_body=True,
            json=lambda: (_ for _ in ()).throw(RuntimeError("bad body")),
        ),
    )
    assert (await server.steer(exploding_request)).status == 400

    steerable_request = cast(
        Any,
        SimpleNamespace(
            match_info={"issue_identifier": "ENG-409"},
            can_read_body=True,
            json=lambda: asyncio.sleep(0, result={"message": "focus"}),
        ),
    )
    assert (await server.steer(steerable_request)).status == 409

    events: list[str] = []

    class FakeRunner:
        async def cleanup(self) -> None:
            events.append("cleanup")

    async def fake_start_runner() -> FakeRunner:
        server._bound_port = 4321
        events.append("started")
        return FakeRunner()

    async def fake_wait(stop_event: asyncio.Event, *, timeout: float | None) -> bool:
        metadata = read_runtime_metadata(str(workflow.resolve()))
        assert metadata is not None
        assert metadata["port"] == 4321
        stop_event.set()
        return True

    monkeypatch.setattr(server, "_start_runner", fake_start_runner)
    monkeypatch.setattr(server, "_wait_for_stop_or_config", fake_wait)
    await server.run(asyncio.Event())
    assert read_runtime_metadata(str(workflow.resolve())) is None
    assert events == ["started", "cleanup"]


@pytest.mark.asyncio
async def test_observability_server_fail_fast_on_startup_raises() -> None:
    class FakeOrchestrator:
        async def snapshot(self) -> dict[str, Any]:
            return {
                "running": [],
                "retrying": [],
                "agent_totals": {},
                "rate_limits": {},
            }

        async def request_refresh(self) -> dict[str, Any]:
            return {"queued": True}

        async def request_steer(
            self, issue_identifier: str, message: str
        ) -> dict[str, Any]:
            return {"accepted": True}

    server = ObservabilityHTTPServer(
        cast(Any, FakeOrchestrator()),
        host="127.0.0.1",
        port=4321,
        fail_fast_on_startup=True,
    )
    with pytest.raises(OSError, match="boom"):

        async def fail_start() -> Any:
            raise OSError("boom")

        server._start_runner = fail_start  # type: ignore[method-assign]
        await server.run(asyncio.Event())


@pytest.mark.asyncio
async def test_client_steer_and_server_disabled_wait_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    client = AppServerClient(settings.coding_agent, settings.workspace)
    session = make_session()

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.steer_turn",
        lambda _session, _message: asyncio.sleep(0, result="turn-1"),
    )
    assert await client.steer(session, "focus") == "turn-1"

    class FakeOrchestrator:
        async def snapshot(self) -> dict[str, Any]:
            return {
                "running": [],
                "retrying": [],
                "agent_totals": {},
                "rate_limits": {},
            }

        async def request_refresh(self) -> dict[str, Any]:
            return {"queued": True}

        async def request_steer(
            self, issue_identifier: str, message: str
        ) -> dict[str, Any]:
            return {"accepted": True}

    server = ObservabilityHTTPServer(
        cast(Any, FakeOrchestrator()), host="127.0.0.1", port=None
    )
    monkeypatch.setattr(
        server,
        "_wait_for_stop_or_config",
        lambda _stop_event, *, timeout: asyncio.sleep(0, result=True),
    )
    await server.run(asyncio.Event())


def test_observability_api_client_and_runtime_metadata_edge_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = ControlEndpoint("127.0.0.1", 4000)
    request = httpx.Request("POST", "http://127.0.0.1:4000")

    monkeypatch.setattr(
        "httpx.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            httpx.ConnectError("bad", request=request)
        ),
    )
    with pytest.raises(ControlRequestError, match="Could not reach Code Factory"):
        steer_issue(endpoint, "ENG-1", "focus")

    monkeypatch.setattr(
        "httpx.post",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=202, content=b"", json=lambda: {}
        ),
    )
    assert steer_issue(endpoint, "ENG-1", "focus") == {}

    monkeypatch.setattr(
        "httpx.post",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=409,
            content=b"{}",
            json=lambda: {
                "error": {"code": "issue_not_steerable", "message": "not steerable"}
            },
        ),
    )
    with pytest.raises(ControlRequestError, match="not steerable"):
        steer_issue(endpoint, "ENG-1", "focus")

    monkeypatch.setattr(
        "httpx.post",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=500,
            content=b"oops",
            json=lambda: "bad",
        ),
    )
    with pytest.raises(ControlRequestError, match="Unexpected control-plane response"):
        steer_issue(endpoint, "ENG-1", "focus")

    path = runtime_metadata_path(str((tmp_path / "WORKFLOW.md").resolve()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad", encoding="utf-8")
    assert read_runtime_metadata(str((tmp_path / "WORKFLOW.md").resolve())) is None
    clear_runtime_metadata(str((tmp_path / "MISSING.md").resolve()))


@pytest.mark.asyncio
async def test_runtime_and_worker_steer_edge_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runtime = CodexRuntime(settings, MemoryTracker([]))
    with pytest.raises(TypeError, match="Unsupported session type"):
        await runtime.steer(object(), "focus")  # type: ignore[arg-type]

    session = make_session()
    runtime._client.steer = lambda _session, _message: asyncio.sleep(  # type: ignore[method-assign]
        0, result="turn-1"
    )
    assert await runtime.steer(session, "focus") == "turn-1"

    snapshot = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    worker = IssueWorker(
        issue=make_issue(identifier="ENG-1"),
        workflow_snapshot=snapshot,
        orchestrator_queue=asyncio.Queue(),
        tracker=MemoryTracker([]),
    )
    with pytest.raises(RuntimeError, match="worker_has_no_active_session"):
        await worker.steer("focus")

    worker._session = cast(Any, SimpleNamespace(stop=lambda: asyncio.sleep(0)))
    with pytest.raises(RuntimeError, match="worker_has_no_agent_runtime"):
        await worker.steer("focus")


def test_service_build_http_server_disabled_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from code_factory.application.service import CodeFactoryService

    snapshot = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    disabled_snapshot = replace(
        snapshot,
        settings=replace(
            snapshot.settings,
            server=replace(snapshot.settings.server, port=None),
        ),
    )
    messages: list[str] = []

    monkeypatch.setattr(
        "code_factory.application.service.LOGGER.info",
        lambda message, *args: messages.append(message % args if args else message),
    )
    service = CodeFactoryService(str(snapshot.path))
    service._build_http_server(disabled_snapshot, cast(Any, object()))
    assert any("disabled" in message.lower() for message in messages)


@pytest.mark.asyncio
async def test_reconciliation_and_session_stop_edge_paths() -> None:
    entry = RunningEntry(
        issue_id="issue-1",
        identifier="ENG-1",
        issue=make_issue(id="issue-1", identifier="ENG-1", state="In Progress"),
        workspace_path="/tmp/workspaces/ENG-1",
        worker=object(),
        started_at=datetime.now(UTC),
        turn_id="turn-1",
    )
    actor = cast(
        Any,
        SimpleNamespace(
            running={"issue-1": entry},
            agent_totals={
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "seconds_running": 0,
            },
            agent_rate_limits=None,
        ),
    )
    from code_factory.runtime.orchestration.reconciliation import ReconciliationMixin

    cast(Any, ReconciliationMixin)._integrate_agent_update(
        actor,
        "issue-1",
        {"event": "turn_completed", "timestamp": datetime.now(UTC)},
    )
    assert entry.turn_id is None

    session = make_session()
    session.routing_task = None
    await session.stop()
