from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from code_factory.coding_agents.base import (
    build_coding_agent_runtime,
    parse_coding_agent_settings,
    validate_coding_agent_settings,
)
from code_factory.coding_agents.codex.app_server.client import AppServerClient
from code_factory.coding_agents.codex.app_server.messages import (
    NON_INTERACTIVE_TOOL_INPUT_ANSWER,
    approval_option_label,
    default_on_message,
    emit_message,
    extract_rate_limits,
    extract_token_usage,
    integer_like,
    integer_token_map,
    map_at_path,
    message_params,
    message_summary,
    metadata_from_message,
    needs_input,
    rate_limits_from_payload,
    tool_call_name,
    tool_request_user_input_approval_answers,
    tool_request_user_input_unavailable_answers,
)
from code_factory.coding_agents.codex.app_server.protocol import (
    INITIALIZE_ID,
    THREAD_START_ID,
    TURN_START_ID,
    await_response,
    send_initialize,
    start_thread,
    start_turn,
)
from code_factory.coding_agents.codex.app_server.session import AppServerSession
from code_factory.coding_agents.codex.app_server.streams import (
    log_non_json_stream_line,
    send_message,
    stderr_reader,
    stdout_reader,
    wait_for_exit,
)
from code_factory.coding_agents.codex.app_server.tool_response import (
    build_tool_response,
)
from code_factory.coding_agents.codex.app_server.turns import (
    approve_or_require,
    await_turn_completion,
    handle_tool_call,
    handle_tool_request_user_input,
    handle_turn_message,
)
from code_factory.coding_agents.codex.config import normalize_approval_policy
from code_factory.coding_agents.codex.runtime import CodexRuntime
from code_factory.coding_agents.codex.tools import DynamicToolExecutor
from code_factory.coding_agents.codex.tools.results import ToolExecutionOutcome
from code_factory.errors import (
    AppServerError,
    ConfigValidationError,
    TrackerClientError,
)
from code_factory.observability.api.payloads import (
    due_at_iso8601,
    humanize_agent_message,
    iso8601,
    issue_payload,
    recent_events_payload,
    retry_entry_payload,
    retry_issue_payload,
    running_entry_payload,
    running_issue_payload,
    state_payload,
)
from code_factory.observability.api.server import (
    ObservabilityHTTPServer,
    site_bound_port,
)
from code_factory.runtime.subprocess.process_tree import ProcessTree
from code_factory.trackers.memory import MemoryTracker

from .conftest import make_issue, make_snapshot, write_workflow_file


def make_settings(tmp_path: Path, *, overrides: dict[str, Any] | None = None):
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", **(overrides or {}))
    return make_snapshot(workflow).settings


def make_stream_reader(*chunks: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


async def noop_handler(_message: dict[str, Any]) -> None:
    return None


def make_session() -> AppServerSession:
    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = SimpleNamespace(write=lambda data: None, drain=self._drain)
            self.stdout = make_stream_reader()
            self.stderr = make_stream_reader()
            self.returncode = 0
            self.pid = 123

        async def _drain(self) -> None:
            return None

        async def wait(self) -> int:
            return 0

    process = FakeProcess()
    process_tree = ProcessTree(process=cast(Any, process), command="cmd", cwd="/tmp")
    loop = asyncio.get_running_loop()
    stdout_task = loop.create_future()
    stdout_task.set_result(None)
    stderr_task = loop.create_future()
    stderr_task.set_result(None)
    wait_task = loop.create_future()
    wait_task.set_result(None)
    return AppServerSession(
        process_tree=process_tree,
        workspace="/tmp/workspace",
        approval_policy="never",
        thread_sandbox="workspace-write",
        turn_sandbox_policy={"type": "workspaceWrite"},
        thread_id="thread-1",
        read_timeout_ms=100,
        turn_timeout_ms=100,
        auto_approve_requests=True,
        stdout_queue=asyncio.Queue(),
        stdout_task=stdout_task,  # type: ignore[arg-type]
        stderr_task=stderr_task,  # type: ignore[arg-type]
        wait_task=wait_task,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_coding_agent_base_wrappers_and_codex_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    tracker = MemoryTracker([])
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.runtime.build_coding_agent_runtime",
        lambda settings, tracker: ("runtime", settings, tracker),
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.config.validate_coding_agent_settings",
        lambda settings: (_ for _ in ()).throw(ConfigValidationError("bad")),
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.config.parse_coding_agent_settings",
        lambda config: {"parsed": config},
    )

    assert build_coding_agent_runtime(settings, tracker) == (
        "runtime",
        settings,
        tracker,
    )
    with pytest.raises(ConfigValidationError, match="bad"):
        validate_coding_agent_settings(settings)
    assert parse_coding_agent_settings({"codex": {}}) == {"parsed": {"codex": {}}}

    parsed = cast(
        Any,
        parse_coding_agent_settings(
            {
                "codex": {
                    "approval_policy": "never",
                    "turn_sandbox_policy": {"readOnly": ["a"]},
                }
            }
        ),
    )
    assert parsed["parsed"] == {
        "codex": {
            "approval_policy": "never",
            "turn_sandbox_policy": {"readOnly": ["a"]},
        }
    }
    default_policy = cast(dict[str, Any], normalize_approval_policy(None))
    assert default_policy["reject"]["rules"] is True
    assert normalize_approval_policy("never") == "never"
    assert normalize_approval_policy({"x": 1}) == {"x": 1}
    with pytest.raises(ConfigValidationError, match="codex.approval_policy"):
        normalize_approval_policy(1)


@pytest.mark.asyncio
async def test_messages_helpers_and_metadata() -> None:
    session = make_session()
    messages: list[dict[str, Any]] = []
    await emit_message(noop_handler, "event", {"payload": 1}, {"meta": 2})
    await default_on_message({})

    async def collect(message: dict[str, Any]) -> None:
        messages.append(message)

    await emit_message(collect, "event", {"payload": 1}, {"meta": 2})
    assert messages[0]["event"] == "event"
    assert messages[0]["meta"] == 2

    payload = {
        "method": "turn/completed",
        "params": {"question": " Approve? "},
        "tokenUsage": {"total": {"inputTokens": 2}},
        "rate_limits": {"limit_id": "rl", "primary": {}},
    }
    metadata = metadata_from_message(session, payload)
    assert metadata["runtime_pid"] == "123"
    assert metadata["token_usage"] == {"inputTokens": 2}
    assert metadata["rate_limits"] == {"limit_id": "rl", "primary": {}}
    assert metadata["message_summary"] == "turn/completed"

    assert message_params({"params": {"x": 1}}) == {"x": 1}
    assert message_params({}) == {}
    assert tool_call_name({"tool": " linear_graphql "}) == "linear_graphql"
    assert tool_call_name({"name": " sync_workpad "}) == "sync_workpad"
    assert tool_call_name({"tool": " "}) is None
    assert needs_input("turn/input_required", {}) is True
    assert needs_input("other", {"params": {"requiresInput": True}}) is True
    assert needs_input("other", {"type": "needs_input"}) is True
    assert needs_input("other", {}) is False
    assert approval_option_label([{"label": "Approve Once"}]) == "Approve Once"
    assert approval_option_label([{"label": "Allow operation"}]) == "Allow operation"
    assert approval_option_label([{"label": "Deny"}]) is None
    assert tool_request_user_input_approval_answers(
        {"questions": [{"id": "q1", "options": [{"label": "Approve this Session"}]}]}
    ) == {"q1": {"answers": ["Approve this Session"]}}
    assert tool_request_user_input_approval_answers({"questions": ["bad"]}) is None
    assert tool_request_user_input_unavailable_answers(
        {"questions": [{"id": "q1"}]}
    ) == {"q1": {"answers": [NON_INTERACTIVE_TOOL_INPUT_ANSWER]}}
    assert tool_request_user_input_unavailable_answers({"questions": ["bad"]}) is None
    assert extract_token_usage(
        {"params": {"msg": {"info": {"total_token_usage": {"totalTokens": 3}}}}}
    ) == {"totalTokens": 3}
    assert extract_token_usage(
        {"method": "turn/completed", "usage": {"prompt_tokens": 2}}
    ) == {"prompt_tokens": 2}
    assert extract_rate_limits({"nested": {"limit_name": "x", "credits": {}}}) == {
        "limit_name": "x",
        "credits": {},
    }
    assert rate_limits_from_payload({"nested": [{"limit_id": "x", "primary": {}}]}) == {
        "limit_id": "x",
        "primary": {},
    }
    assert map_at_path({"a": {"b": 1}}, ("a", "b")) == 1
    assert map_at_path({"a": 1}, ("a", "b")) is None
    assert integer_token_map({"inputTokens": "3"}) is True
    assert integer_token_map({"x": 1}) is False
    assert integer_like(" 5 ") == 5
    assert integer_like(-1) is None
    assert message_summary({"params": {"question": " What now? "}}) == "What now?"
    assert message_summary(" hello ") == "hello"


@pytest.mark.asyncio
async def test_streams_helpers(caplog: pytest.LogCaptureFixture) -> None:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    await stdout_reader(make_stream_reader(b"one\npart", b"ial\r\ntwo"), queue)
    assert await queue.get() == ("line", "one")
    assert await queue.get() == ("line", "partial")
    assert await queue.get() == ("line", "two")

    with caplog.at_level(logging.DEBUG):
        await stderr_reader(make_stream_reader(b"warning here\n", b"note"))
    assert any("warning here" in record.message for record in caplog.records)

    queue = asyncio.Queue()

    class FakeProcessTree:
        async def wait(self) -> int:
            return 9

    await wait_for_exit(FakeProcessTree(), queue)  # type: ignore[arg-type]
    assert await queue.get() == ("exit", 9)

    class FakeStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None

    stdin = FakeStdin()
    process_tree = ProcessTree(
        process=cast(Any, SimpleNamespace(stdin=stdin, pid=1)),
        command="cmd",
        cwd="/tmp",
    )
    await send_message(process_tree, {"id": 1, "method": "ping"})
    assert json.loads(stdin.writes[0].decode().strip()) == {"id": 1, "method": "ping"}

    with caplog.at_level(logging.DEBUG):
        log_non_json_stream_line("plain info", "stdout")
        log_non_json_stream_line("fatal failure", "stderr")
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.asyncio
async def test_protocol_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    await queue.put(("line", "not-json"))
    await queue.put(("line", json.dumps({"id": 999, "result": {"skip": True}})))
    await queue.put(("line", json.dumps({"id": 1, "result": {"ok": True}})))
    assert await await_response(queue, 1, timeout_ms=50, default_timeout_ms=50) == {
        "ok": True
    }

    queue = asyncio.Queue()
    await queue.put(("exit", 4))
    with pytest.raises(AppServerError, match="port_exit"):
        await await_response(queue, 1, timeout_ms=50, default_timeout_ms=50)

    queue = asyncio.Queue()
    await queue.put(("line", json.dumps({"id": 1, "error": {"message": "bad"}})))
    with pytest.raises(AppServerError, match="response_error"):
        await await_response(queue, 1, timeout_ms=50, default_timeout_ms=50)

    queue = asyncio.Queue()
    await queue.put(("line", json.dumps({"id": 1, "result": []})))
    with pytest.raises(AppServerError, match="response_error"):
        await await_response(queue, 1, timeout_ms=50, default_timeout_ms=50)

    sent: list[dict[str, Any]] = []

    async def fake_send_message(_process_tree: Any, message: dict[str, Any]) -> None:
        sent.append(message)

    async def fake_await_response(*args: Any, **kwargs: Any) -> dict[str, Any]:
        request_id = args[1]
        if request_id == THREAD_START_ID:
            return {"thread": {"id": "thread-1"}}
        if request_id == TURN_START_ID:
            return {"turn": {"id": "turn-1"}}
        return {}

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.send_message",
        fake_send_message,
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.await_response",
        fake_await_response,
    )
    process_tree = ProcessTree(
        process=cast(
            Any,
            SimpleNamespace(
                stdin=SimpleNamespace(
                    write=lambda _: None, drain=lambda: asyncio.sleep(0)
                ),
                pid=1,
            ),
        ),
        command="cmd",
        cwd="/tmp",
    )
    queue = asyncio.Queue()
    await send_initialize(queue, process_tree, default_timeout_ms=100)
    assert sent[0]["method"] == "initialize"
    assert sent[1]["method"] == "initialized"

    assert (
        await start_thread(
            queue,
            process_tree,
            "/tmp",
            "never",
            "workspace-write",
            default_timeout_ms=100,
        )
        == "thread-1"
    )
    session = make_session()
    assert await start_turn(session, "prompt", make_issue()) == "turn-1"


@pytest.mark.asyncio
async def test_turn_helpers() -> None:
    session = make_session()
    events: list[str] = []
    sent: list[dict[str, Any]] = []

    async def on_message(message: dict[str, Any]) -> None:
        events.append(message["event"])

    async def fake_send_message(_process_tree: Any, payload: dict[str, Any]) -> None:
        sent.append(payload)

    session.process_tree = ProcessTree(
        process=cast(
            Any,
            SimpleNamespace(
                stdin=SimpleNamespace(
                    write=lambda _: None, drain=lambda: asyncio.sleep(0)
                ),
                pid=1,
            ),
        ),
        command="cmd",
        cwd="/tmp",
    )
    original_send = send_message
    try:
        import code_factory.coding_agents.codex.app_server.turns as turns_module

        turns_module.send_message = fake_send_message  # type: ignore[assignment]
        executor = DynamicToolExecutor(
            lambda query, variables: asyncio.sleep(0, result={"ok": True})
        )

        assert (
            await handle_turn_message(
                session,
                on_message,
                executor,
                {"method": "turn/completed", "params": {}},
                "{}",
            )
            == "turn_completed"
        )
        with pytest.raises(AppServerError, match="turn_failed"):
            await handle_turn_message(
                session,
                on_message,
                executor,
                {"method": "turn/failed", "params": {"reason": "bad"}},
                "{}",
            )
        with pytest.raises(AppServerError, match="turn_cancelled"):
            await handle_turn_message(
                session,
                on_message,
                executor,
                {"method": "turn/cancelled", "params": {"reason": "stop"}},
                "{}",
            )

        assert (
            await approve_or_require(
                session,
                on_message,
                {"id": 5, "method": "applyPatchApproval"},
                "{}",
                {},
                "approved_for_session",
            )
            == "continue"
        )
        assert sent[-1]["result"]["decision"] == "approved_for_session"

        non_auto = make_session()
        non_auto.auto_approve_requests = False
        with pytest.raises(AppServerError, match="approval_required"):
            await approve_or_require(
                non_auto,
                on_message,
                {"id": 6, "method": "applyPatchApproval"},
                "{}",
                {},
                "approved_for_session",
            )

        assert (
            await handle_tool_request_user_input(
                session,
                on_message,
                {
                    "id": 7,
                    "params": {
                        "questions": [
                            {"id": "q1", "options": [{"label": "Approve this Session"}]}
                        ]
                    },
                },
                "{}",
                {},
            )
            == "continue"
        )
        session.auto_approve_requests = False
        assert (
            await handle_tool_request_user_input(
                session,
                on_message,
                {"id": 8, "params": {"questions": [{"id": "q1"}]}},
                "{}",
                {},
            )
            == "continue"
        )
        with pytest.raises(AppServerError, match="turn_input_required"):
            await handle_tool_request_user_input(
                session,
                on_message,
                {"id": 9, "params": {}},
                "{}",
                {},
            )

        failing_executor = DynamicToolExecutor(
            lambda query, variables: asyncio.sleep(
                0, result={"errors": [{"message": "bad"}]}
            )
        )
        assert (
            await handle_tool_call(
                session,
                on_message,
                {
                    "id": 10,
                    "params": {
                        "tool": "linear_graphql",
                        "arguments": {"query": "query"},
                    },
                },
                "{}",
                {},
                failing_executor,
            )
            == "continue"
        )
        assert events[-1] == "tool_call_failed"

        session.stdout_queue.put_nowait(("line", "not-json"))
        session.stdout_queue.put_nowait(
            ("line", json.dumps({"method": "turn/completed"}))
        )
        assert (
            await await_turn_completion(session, on_message, executor)
            == "turn_completed"
        )
    finally:
        import code_factory.coding_agents.codex.app_server.turns as turns_module

        turns_module.send_message = original_send  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_client_runtime_and_observability_behaviors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        tmp_path, overrides={"workspace": {"root": str(tmp_path / "workspaces")}}
    )
    issue = make_issue(identifier="ENG-1")
    client = AppServerClient(settings)

    workspace = tmp_path / "workspaces" / "ENG-1"
    workspace.mkdir(parents=True)
    assert client._validate_workspace_cwd(str(workspace)) == str(workspace.resolve())
    with pytest.raises(AppServerError, match="invalid_workspace_cwd"):
        client._validate_workspace_cwd(str(tmp_path))
    assert client._resolve_turn_sandbox_policy(str(workspace))["writableRoots"] == [
        str(workspace.resolve())
    ]

    settings_with_policy = make_settings(
        tmp_path,
        overrides={"codex": {"turn_sandbox_policy": {"type": "custom"}}},
    )
    custom_client = AppServerClient(settings_with_policy)
    assert custom_client._resolve_turn_sandbox_policy(str(workspace)) == {
        "type": "custom"
    }

    executor = client._build_tool_executor(str(workspace))
    outcome = await executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert outcome.event == "tool_call_completed"
    assert outcome.success is False

    emitted: list[str] = []

    async def on_message(message: dict[str, Any]) -> None:
        emitted.append(message["event"])

    session = make_session()
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.start_turn",
        lambda session, prompt, issue: asyncio.sleep(0, result="turn-1"),
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.await_turn_completion",
        lambda session, handler, executor: asyncio.sleep(0, result="turn_completed"),
    )
    result = await client.run_turn(session, "prompt", issue, on_message=on_message)
    assert result["session_id"] == "thread-1-turn-1"
    assert emitted[0] == "session_started"

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.await_turn_completion",
        lambda session, handler, executor: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        await client.run_turn(session, "prompt", issue, on_message=on_message)
    assert emitted[-1] == "turn_ended_with_error"

    class GraphQLTracker(MemoryTracker):
        def __init__(self) -> None:
            super().__init__([])

        async def graphql(
            self, query: str, variables: dict[str, Any]
        ) -> dict[str, Any]:
            return {"data": {"ok": True}}

    runtime = CodexRuntime(settings, GraphQLTracker())
    tool_executor = runtime._build_dynamic_tool_executor(str(workspace))
    outcome = await tool_executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert outcome.success is True
    with pytest.raises(TypeError, match="Unsupported session type"):
        await runtime.run_turn(object(), "prompt", issue)  # type: ignore[arg-type]

    snapshot = {
        "running": [
            {
                "issue_id": "1",
                "identifier": "ENG-1",
                "state": "In Progress",
                "session_id": "thread-1",
                "turn_count": 2,
                "last_agent_event": "tool_call_completed",
                "last_agent_message": {"message": "done"},
                "started_at": datetime(2024, 1, 1, tzinfo=UTC),
                "last_agent_timestamp": datetime(2024, 1, 1, 0, 1, tzinfo=UTC),
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
                "runtime_pid": "123",
                "workspace_path": "/tmp/workspace",
            }
        ],
        "retrying": [
            {
                "issue_id": "2",
                "identifier": "ENG-2",
                "attempt": 2,
                "due_in_ms": 5000,
                "error": "boom",
                "workspace_path": "/tmp/retry",
            }
        ],
        "agent_totals": {"total_tokens": 3},
        "rate_limits": {"primary": {}},
    }
    assert state_payload(snapshot)["counts"] == {"running": 1, "retrying": 1}
    running_issue = issue_payload("ENG-1", snapshot)
    retry_issue = issue_payload("ENG-2", snapshot)
    assert running_issue is not None and running_issue["status"] == "running"
    assert retry_issue is not None and retry_issue["status"] == "retrying"
    assert issue_payload("missing", snapshot) is None
    assert humanize_agent_message({"message": "done"}) == "done"
    assert (
        iso8601(datetime(2024, 1, 1, 0, 0, 1, 123456, tzinfo=UTC))
        == "2024-01-01T00:00:01Z"
    )
    assert due_at_iso8601(1000) is not None
    assert due_at_iso8601("bad") is None
    assert recent_events_payload(snapshot["running"][0])[0]["message"] == "done"
    assert running_entry_payload(snapshot["running"][0])["runtime_pid"] == "123"
    assert retry_entry_payload(snapshot["retrying"][0])["attempt"] == 2
    assert running_issue_payload(snapshot["running"][0])["turn_count"] == 2
    assert retry_issue_payload(snapshot["retrying"][0])["attempt"] == 2

    class FakeOrchestrator:
        async def snapshot(self) -> dict[str, Any]:
            return snapshot

        async def request_refresh(self) -> dict[str, Any]:
            return {"queued": True}

    server = ObservabilityHTTPServer(
        cast(Any, FakeOrchestrator()), host="127.0.0.1", port=9999
    )
    state_response = await server.state(None)  # type: ignore[arg-type]
    issue_response = await server.issue(
        cast(Any, SimpleNamespace(match_info={"issue_identifier": "ENG-1"}))
    )
    not_found_response = await server.issue(
        cast(Any, SimpleNamespace(match_info={"issue_identifier": "missing"}))
    )
    refresh_response = await server.refresh(None)  # type: ignore[arg-type]
    fallback_response = await server.not_found(None)  # type: ignore[arg-type]
    assert state_response.status == 200
    assert issue_response.status == 200
    assert not_found_response.status == 404
    assert refresh_response.status == 202
    assert fallback_response.status == 404
    assert site_bound_port(cast(Any, SimpleNamespace(_server=None))) is None
    assert (
        site_bound_port(cast(Any, SimpleNamespace(_server=SimpleNamespace(sockets=[]))))
        is None
    )
    socket = SimpleNamespace(getsockname=lambda: ("127.0.0.1", 3210))
    assert (
        site_bound_port(
            cast(Any, SimpleNamespace(_server=SimpleNamespace(sockets=[socket])))
        )
        == 3210
    )


@pytest.mark.asyncio
async def test_observability_server_run_retries_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

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

    class FakeRunner:
        async def cleanup(self) -> None:
            events.append("cleanup")

    server = ObservabilityHTTPServer(
        cast(Any, FakeOrchestrator()), host="127.0.0.1", port=9999
    )
    stop_event = asyncio.Event()
    attempts = 0

    async def fake_start_runner() -> FakeRunner:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("boom")
        events.append("started")
        asyncio.get_running_loop().call_soon(stop_event.set)
        return FakeRunner()

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    monkeypatch.setattr(server, "_start_runner", fake_start_runner)
    monkeypatch.setattr(
        "code_factory.observability.api.server.asyncio.wait_for", fake_wait_for
    )
    await server.run(stop_event)
    assert events == ["started", "cleanup"]
