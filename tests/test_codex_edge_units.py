from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from symphony.coding_agents.codex.app_server.client import AppServerClient
from symphony.coding_agents.codex.app_server.messages import (
    approval_option_label,
    message_summary,
    metadata_from_message,
    rate_limits_from_payload,
    tool_request_user_input_approval_answers,
    tool_request_user_input_unavailable_answers,
)
from symphony.coding_agents.codex.app_server.protocol import (
    await_response,
    start_thread,
    start_turn,
)
from symphony.coding_agents.codex.app_server.session import AppServerSession
from symphony.coding_agents.codex.app_server.turns import (
    await_turn_completion,
    handle_tool_call,
    handle_tool_request_user_input,
    handle_turn_message,
)
from symphony.coding_agents.codex.config import (
    parse_coding_agent_settings,
    validate_coding_agent_settings,
)
from symphony.coding_agents.codex.runtime import CodexRuntime
from symphony.coding_agents.codex.tools.executor import (
    SYNC_WORKPAD_CREATE,
    SYNC_WORKPAD_UPDATE,
    encode_payload,
    normalize_linear_graphql_arguments,
    normalize_sync_workpad_args,
    read_workpad_file,
    sync_workpad_request,
    tool_error_payload,
)
from symphony.errors import AppServerError, TrackerClientError, WorkflowLoadError
from symphony.prompts import build_prompt
from symphony.workflow.loader import load_workflow

from .conftest import make_issue, make_snapshot, write_workflow_file


def make_settings(tmp_path: Path, *, overrides: dict[str, Any] | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", **(overrides or {}))
    return make_snapshot(workflow).settings


def make_stream_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    return reader


class FakeProcess:
    def __init__(self, pid: int | None = 123) -> None:
        self.pid = pid
        self.returncode = 0
        self.stdout = make_stream_reader()
        self.stderr = make_stream_reader()
        self.stdin = SimpleNamespace(write=lambda data: None, drain=self._drain)

    async def _drain(self) -> None:
        return None

    async def wait(self) -> int:
        return 0


def make_session(
    *, auto_approve_requests: bool = True, pid: int | None = 123
) -> AppServerSession:
    process = FakeProcess(pid=pid)
    process_tree = cast(
        Any,
        SimpleNamespace(
            process=process,
            pid=pid,
            terminate=_async_noop,
        ),
    )
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
        approval_policy="never" if auto_approve_requests else {"reject": {}},
        thread_sandbox="workspace-write",
        turn_sandbox_policy={"type": "workspaceWrite"},
        thread_id="thread-1",
        read_timeout_ms=50,
        turn_timeout_ms=50,
        auto_approve_requests=auto_approve_requests,
        stdout_queue=asyncio.Queue(),
        stdout_task=stdout_task,  # type: ignore[arg-type]
        stderr_task=stderr_task,  # type: ignore[arg-type]
        wait_task=wait_task,  # type: ignore[arg-type]
    )


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def collect_messages(message: dict[str, Any], sink: list[dict[str, Any]]) -> None:
    sink.append(message)


@pytest.mark.asyncio
async def test_dynamic_tool_executor_validation_and_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert normalize_linear_graphql_arguments(" query { viewer { id } } ") == (
        "query { viewer { id } }",
        {},
    )
    with pytest.raises(ValueError, match="missing_query"):
        normalize_linear_graphql_arguments("  ")
    with pytest.raises(ValueError, match="missing_query"):
        normalize_linear_graphql_arguments({"query": ""})
    with pytest.raises(TypeError, match="invalid_variables"):
        normalize_linear_graphql_arguments({"query": "query", "variables": [1]})
    with pytest.raises(TypeError, match="invalid_arguments"):
        normalize_linear_graphql_arguments(7)

    assert normalize_sync_workpad_args(
        {"issue_id": "ENG-1", "file_path": "/tmp/workpad.md", "comment_id": ""}
    ) == ("ENG-1", "/tmp/workpad.md", None)
    with pytest.raises(ValueError, match="issue_id"):
        normalize_sync_workpad_args({"file_path": "/tmp/workpad.md"})
    with pytest.raises(ValueError, match="file_path"):
        normalize_sync_workpad_args({"issue_id": "ENG-1"})
    with pytest.raises(ValueError, match="required"):
        normalize_sync_workpad_args("bad")

    assert sync_workpad_request("ENG-1", None, "body") == (
        SYNC_WORKPAD_CREATE,
        {"issueId": "ENG-1", "body": "body"},
    )
    assert sync_workpad_request("ENG-1", "comment-1", "body") == (
        SYNC_WORKPAD_UPDATE,
        {"id": "comment-1", "body": "body"},
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workpad = workspace / "workpad.md"
    workpad.write_text("notes\n", encoding="utf-8")
    assert read_workpad_file("workpad.md", (str(workspace),)) == "notes\n"

    empty = workspace / "empty.md"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="file is empty"):
        read_workpad_file(str(empty), (str(workspace),))

    outside = tmp_path / "outside.md"
    outside.write_text("forbidden\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the allowed workspace roots"):
        read_workpad_file(str(outside), (str(workspace),))

    missing = workspace / "missing.md"
    with pytest.raises(ValueError, match="cannot read"):
        read_workpad_file(str(missing), (str(workspace),))

    monkeypatch.setattr(
        "symphony.coding_agents.codex.tools.executor.canonicalize",
        lambda _path: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(ValueError, match="cannot read"):
        read_workpad_file(str(workpad), (str(workspace),))

    assert json.loads(encode_payload({"a": 1})) == {"a": 1}
    assert encode_payload("raw") == "'raw'"

    assert tool_error_payload(ValueError("missing_query")) == {
        "error": {"message": "`linear_graphql` requires a non-empty `query` string."}
    }
    assert tool_error_payload(TypeError("invalid_arguments")) == {
        "error": {
            "message": "`linear_graphql` expects either a GraphQL query string or an object with `query` and optional `variables`."
        }
    }
    assert tool_error_payload(TypeError("invalid_variables")) == {
        "error": {
            "message": "`linear_graphql.variables` must be a JSON object when provided."
        }
    }
    assert tool_error_payload(TrackerClientError("missing_linear_api_token")) == {
        "error": {
            "message": "Symphony is missing Linear auth. Set `linear.api_key` in `WORKFLOW.md` or export `LINEAR_API_KEY`."
        }
    }
    assert tool_error_payload(ValueError(("other", "x"))) == {
        "error": {
            "message": "Linear GraphQL tool execution failed.",
            "reason": "\"('other', 'x')\"",
        }
    }
    assert tool_error_payload(RuntimeError("boom")) == {
        "error": {
            "message": "Linear GraphQL tool execution failed.",
            "reason": "'boom'",
        }
    }


@pytest.mark.asyncio
async def test_turn_handlers_cover_stream_and_input_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingExecutor:
        async def execute(
            self, _tool: str | None, _arguments: Any
        ) -> tuple[dict[str, Any], str]:
            return {"success": False, "contentItems": []}, "tool_call_completed"

    class SuccessExecutor:
        async def execute(
            self, _tool: str | None, _arguments: Any
        ) -> tuple[dict[str, Any], str]:
            return {"success": True, "contentItems": []}, "tool_call_completed"

    sent_messages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "symphony.coding_agents.codex.app_server.turns.send_message",
        lambda _process_tree, payload: sent_messages.append(payload) or _async_noop(),
    )

    session = make_session()
    messages: list[dict[str, Any]] = []
    await session.stdout_queue.put(("stderr", "ignored"))
    await session.stdout_queue.put(("line", "not-json"))
    await session.stdout_queue.put(("line", json.dumps({"method": "turn/completed"})))
    result = await await_turn_completion(
        session,
        lambda message: collect_messages(message, messages),
        cast(Any, FailingExecutor()),
    )
    assert result == "turn_completed"
    assert [message["event"] for message in messages] == ["malformed", "turn_completed"]

    exit_session = make_session()
    await exit_session.stdout_queue.put(("exit", 9))
    with pytest.raises(AppServerError, match="port_exit"):
        await await_turn_completion(
            exit_session,
            lambda message: collect_messages(message, []),
            cast(Any, FailingExecutor()),
        )

    messages = []
    assert (
        await handle_turn_message(
            session,
            lambda message: collect_messages(message, messages),
            cast(Any, FailingExecutor()),
            {"id": 1},
            "{}",
        )
        == "continue"
    )
    assert messages[-1]["event"] == "other_message"

    messages = []
    blocked_session = make_session(auto_approve_requests=False)
    with pytest.raises(AppServerError, match="approval_required"):
        await handle_turn_message(
            blocked_session,
            lambda message: collect_messages(message, messages),
            cast(Any, FailingExecutor()),
            {"method": "execCommandApproval", "id": 1, "params": {}},
            "{}",
        )
    assert messages[-1]["event"] == "approval_required"

    messages = []
    assert (
        await handle_turn_message(
            session,
            lambda message: collect_messages(message, messages),
            cast(Any, SuccessExecutor()),
            {"method": "item/commandExecution/requestApproval", "id": 9, "params": {}},
            "{}",
        )
        == "continue"
    )
    assert sent_messages[-1]["result"]["decision"] == "acceptForSession"
    assert messages[-1]["event"] == "approval_auto_approved"

    messages = []
    assert (
        await handle_turn_message(
            session,
            lambda message: collect_messages(message, messages),
            cast(Any, SuccessExecutor()),
            {
                "method": "item/tool/requestUserInput",
                "id": 10,
                "params": {
                    "questions": [{"id": "q1", "options": [{"label": "Approve Once"}]}]
                },
            },
            "{}",
        )
        == "continue"
    )
    assert messages[-1]["event"] == "approval_auto_approved"

    messages = []
    assert (
        await handle_turn_message(
            session,
            lambda message: collect_messages(message, messages),
            cast(Any, SuccessExecutor()),
            {"method": "item/unknown", "id": 11, "params": {}},
            "{}",
        )
        == "continue"
    )
    assert messages[-1]["event"] == "notification"

    messages = []
    await handle_tool_call(
        session,
        lambda message: collect_messages(message, messages),
        {"id": 2, "params": {"tool": "linear_graphql", "arguments": {}}},
        "{}",
        {"runtime_pid": "123"},
        cast(Any, FailingExecutor()),
    )
    assert sent_messages[-1] == {
        "id": 2,
        "result": {"success": False, "contentItems": []},
    }
    assert messages[-1]["event"] == "tool_call_failed"

    messages = []
    await handle_tool_call(
        session,
        lambda message: collect_messages(message, messages),
        {"id": 12, "params": {"tool": "linear_graphql", "arguments": {}}},
        "{}",
        {"runtime_pid": "123"},
        cast(Any, SuccessExecutor()),
    )
    assert messages[-1]["event"] == "tool_call_completed"

    messages = []
    payload = {
        "id": 3,
        "params": {
            "questions": [{"id": "approval", "options": [{"label": "Approve Once"}]}]
        },
    }
    assert (
        await handle_tool_request_user_input(
            session,
            lambda message: collect_messages(message, messages),
            payload,
            "{}",
            {"runtime_pid": "123"},
        )
        == "continue"
    )
    assert sent_messages[-1]["result"]["answers"]["approval"]["answers"] == [
        "Approve Once"
    ]
    assert messages[-1]["event"] == "approval_auto_approved"

    messages = []
    unavailable_session = make_session(auto_approve_requests=False)
    assert (
        await handle_tool_request_user_input(
            unavailable_session,
            lambda message: collect_messages(message, messages),
            {"id": 4, "params": {"questions": [{"id": "freeform"}]}},
            "{}",
            {"runtime_pid": "123"},
        )
        == "continue"
    )
    assert messages[-1]["event"] == "tool_input_auto_answered"

    with pytest.raises(AppServerError, match="turn_input_required"):
        await handle_tool_request_user_input(
            session,
            lambda message: collect_messages(message, []),
            {"id": 5, "params": {"questions": "bad"}},
            "{}",
            {"runtime_pid": "123"},
        )


@pytest.mark.asyncio
async def test_protocol_and_client_bootstrap_edge_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    await queue.put(("stderr", "ignored"))
    await queue.put(("line", json.dumps({"id": 1, "result": {"ok": True}})))
    assert await await_response(queue, 1, timeout_ms=10, default_timeout_ms=10) == {
        "ok": True
    }

    queue = asyncio.Queue()
    await queue.put(("line", json.dumps({"id": 2, "result": {"thread": {}}})))
    process_tree = cast(Any, SimpleNamespace(process=FakeProcess()))
    with pytest.raises(AppServerError, match="invalid_thread_payload"):
        await start_thread(
            queue,
            process_tree,
            "/tmp",
            "never",
            "workspace-write",
            default_timeout_ms=10,
        )

    queue = asyncio.Queue()
    session = make_session()
    session.stdout_queue = queue
    await queue.put(("line", json.dumps({"id": 3, "result": {"turn": {}}})))
    with pytest.raises(AppServerError, match="invalid_turn_payload"):
        await start_turn(session, "prompt", make_issue())

    settings = make_settings(tmp_path)
    client = AppServerClient(settings)

    class BootstrapProcessTree:
        def __init__(self) -> None:
            self.process = FakeProcess()
            self.terminated = 0

        async def terminate(self) -> None:
            self.terminated += 1

    async def sleeping_reader(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(
        "symphony.coding_agents.codex.app_server.client.stdout_reader",
        sleeping_reader,
    )
    monkeypatch.setattr(
        "symphony.coding_agents.codex.app_server.client.stderr_reader",
        sleeping_reader,
    )
    monkeypatch.setattr(
        "symphony.coding_agents.codex.app_server.client.wait_for_exit",
        sleeping_reader,
    )

    async def fail_initialize(*_args: Any, **_kwargs: Any) -> None:
        raise AppServerError("boom")

    monkeypatch.setattr(
        "symphony.coding_agents.codex.app_server.client.send_initialize",
        fail_initialize,
    )

    process_tree = BootstrapProcessTree()
    with pytest.raises(AppServerError, match="boom"):
        await client._bootstrap_session(cast(Any, process_tree), "/tmp/workspace")
    assert process_tree.terminated == 1

    fallback_executor = client._build_tool_executor("/tmp/workspace")
    result, event = await fallback_executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert event == "tool_call_completed"
    assert result["success"] is False
    assert "Linear GraphQL tool execution failed." in result["contentItems"][0]["text"]

    explicit_settings = make_settings(
        tmp_path / "explicit",
        overrides={"codex": {"turn_sandbox_policy": {"type": "dangerouslyBypass"}}},
    )
    explicit_client = AppServerClient(explicit_settings)
    assert explicit_client._resolve_turn_sandbox_policy("/tmp/workspace") == {
        "type": "dangerouslyBypass"
    }


@pytest.mark.asyncio
async def test_messages_runtime_and_loader_edge_paths(tmp_path: Path) -> None:
    session = make_session(pid=None)
    metadata = metadata_from_message(session, {"params": {"question": 7}})
    assert metadata == {}
    assert rate_limits_from_payload(
        [{"nested": {"limit_name": "rl", "credits": {}}}]
    ) == {
        "limit_name": "rl",
        "credits": {},
    }
    assert rate_limits_from_payload([{}, {"limit_id": "rl", "primary": {}}]) == {
        "limit_id": "rl",
        "primary": {},
    }
    assert tool_request_user_input_approval_answers({}) is None
    assert (
        tool_request_user_input_approval_answers(
            {"questions": [{"id": "approval", "options": "bad"}]}
        )
        is None
    )
    assert (
        tool_request_user_input_approval_answers(
            {"questions": [{"id": "approval", "options": [{"label": "Deny"}]}]}
        )
        is None
    )
    assert tool_request_user_input_unavailable_answers({}) is None
    assert approval_option_label([{"label": " decline "}]) is None
    assert message_summary({"params": {"question": 7}}) is None
    assert message_summary({}) is None
    assert message_summary(7) is None

    invalid = write_workflow_file(
        tmp_path / "INVALID_PROMPT_WORKFLOW.md",
        prompt="{% if issue.identifier %}",
    )
    with pytest.raises(RuntimeError, match="template_parse_error:"):
        build_prompt(make_issue(), make_snapshot(invalid))

    with pytest.raises(WorkflowLoadError, match="missing_workflow_file"):
        load_workflow(str(tmp_path / "missing.md"))

    settings = make_settings(tmp_path / "codex-blank")
    blank_settings = replace(
        settings, coding_agent=replace(settings.coding_agent, command="")
    )
    with pytest.raises(Exception, match="codex.command can't be blank"):
        validate_coding_agent_settings(blank_settings)
    with pytest.raises(Exception, match="codex.turn_sandbox_policy must be an object"):
        parse_coding_agent_settings({"codex": {"turn_sandbox_policy": 7}})


@pytest.mark.asyncio
async def test_codex_runtime_dynamic_tool_executor_supports_sync_and_async_graphql(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)

    class SyncTracker:
        def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            return {"data": {"query": query, "variables": variables}}

    sync_executor = CodexRuntime(
        settings, cast(Any, SyncTracker())
    )._build_dynamic_tool_executor("/tmp/workspace")
    sync_result, _event = await sync_executor.execute(
        "linear_graphql",
        {"query": "query Viewer { viewer { id } }", "variables": {"ok": True}},
    )
    assert sync_result["success"] is True

    class AsyncTracker:
        async def graphql(
            self, query: str, variables: dict[str, Any]
        ) -> dict[str, Any]:
            return {"data": {"query": query, "variables": variables}}

    async_executor = CodexRuntime(
        settings, cast(Any, AsyncTracker())
    )._build_dynamic_tool_executor("/tmp/workspace")
    async_result, _event = await async_executor.execute(
        "linear_graphql",
        {"query": "query Viewer { viewer { id } }", "variables": {"ok": True}},
    )
    assert async_result["success"] is True

    class BrokenTracker:
        def graphql(self, _query: str, _variables: dict[str, Any]) -> list[str]:
            return ["not-a-dict"]

    broken_executor = CodexRuntime(
        settings, cast(Any, BrokenTracker())
    )._build_dynamic_tool_executor("/tmp/workspace")
    broken_result, _event = await broken_executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert broken_result["success"] is False
    payload = json.loads(broken_result["contentItems"][0]["text"])
    assert payload["error"]["message"].startswith("Symphony is missing Linear auth.")

    class MissingGraphqlTracker:
        graphql = None

    missing_executor = CodexRuntime(
        settings, cast(Any, MissingGraphqlTracker())
    )._build_dynamic_tool_executor("/tmp/workspace")
    missing_result, _event = await missing_executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert missing_result["success"] is False
