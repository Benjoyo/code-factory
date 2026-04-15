from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
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
from code_factory.coding_agents.codex.app_server.correlation import (
    correlate_message,
    extract_message_thread_id,
    extract_message_turn_id,
)
from code_factory.coding_agents.codex.app_server.error_details import (
    format_error_details,
)
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
from code_factory.coding_agents.codex.app_server.policies import (
    resolve_turn_sandbox_policy,
    validate_workspace_cwd,
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
from code_factory.coding_agents.codex.app_server.structured_output import (
    extract_structured_turn_result,
    message_item,
    parse_json_string,
    structured_result_candidates,
    turn_payload,
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
from code_factory.coding_agents.codex.config import (
    build_launch_command,
    normalize_approval_policy,
    repo_skill_disable_config,
)
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
from code_factory.structured_results import (
    StructuredTurnResult,
    structured_turn_output_schema,
)
from code_factory.trackers.memory import MemoryTracker

from ..conftest import make_issue, make_snapshot, write_workflow_file


def make_settings(tmp_path: Path, *, overrides: dict[str, Any] | None = None):
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", **(overrides or {}))
    return make_snapshot(workflow).settings


def create_repo_skill(workspace: Path, name: str) -> Path:
    skill_dir = workspace / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n", encoding="utf-8"
    )
    return skill_dir


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
        event_queue=asyncio.Queue(),
        stdout_task=stdout_task,  # type: ignore[arg-type]
        stderr_task=stderr_task,  # type: ignore[arg-type]
        wait_task=wait_task,  # type: ignore[arg-type]
    )


def test_format_error_details_uses_repr_for_blank_exceptions() -> None:
    class SilentError(Exception):
        def __str__(self) -> str:
            return ""

    assert format_error_details(SilentError()) == "SilentError()"


@pytest.mark.asyncio
async def test_coding_agent_base_wrappers_and_codex_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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


def test_build_launch_command_injects_model_and_reasoning_before_app_server(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="codex -c shell_environment_policy.inherit=all app-server",
            model="gpt-5.3-codex",
            reasoning_effort="xhigh",
        )
    )
    assert (
        launch_command == "codex -c shell_environment_policy.inherit=all --config "
        "model_reasoning_effort=xhigh --model gpt-5.3-codex app-server"
    )


def test_build_launch_command_supports_wrapped_app_server_command(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="/tmp/fake-codex app-server",
            model="gpt-5.3-codex",
            reasoning_effort="high",
        )
    )
    assert (
        launch_command
        == "/tmp/fake-codex --config model_reasoning_effort=high --model "
        "gpt-5.3-codex app-server"
    )


def test_build_launch_command_supports_model_only_override(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="codex app-server",
            model="gpt-5.3-codex",
        )
    )
    assert launch_command == "codex --model gpt-5.3-codex app-server"


def test_build_launch_command_supports_reasoning_only_override(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="codex app-server",
            reasoning_effort="high",
        )
    )
    assert launch_command == "codex --config model_reasoning_effort=high app-server"


def test_build_launch_command_ignores_fast_mode_for_cli_injection(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="codex app-server",
            fast_mode=True,
        )
    )
    assert launch_command == "codex app-server"


def test_build_launch_command_disables_repo_skills_not_in_allowlist(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    create_repo_skill(workspace, "commit")
    disabled_skill = create_repo_skill(workspace, "push")

    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="codex app-server",
            repo_skill_allowlist=("commit",),
        ),
        str(workspace),
    )

    expected_config = repo_skill_disable_config((str(disabled_skill / "SKILL.md"),))
    assert launch_command == f"codex --config '{expected_config}' app-server"


def test_build_launch_command_disables_all_repo_skills_for_empty_allowlist(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    commit_skill = create_repo_skill(workspace, "commit")
    push_skill = create_repo_skill(workspace, "push")

    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="codex app-server",
            repo_skill_allowlist=(),
        ),
        str(workspace),
    )

    expected_config = repo_skill_disable_config(
        (
            str(commit_skill / "SKILL.md"),
            str(push_skill / "SKILL.md"),
        )
    )
    assert launch_command == f"codex --config '{expected_config}' app-server"


def test_build_launch_command_keeps_existing_behavior_without_repo_skills(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="codex app-server",
            repo_skill_allowlist=(),
        ),
        str(workspace),
    )

    assert launch_command == "codex app-server"


def test_build_launch_command_requires_workspace_for_repo_skill_filter(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)

    with pytest.raises(ConfigValidationError, match="workspace is required"):
        build_launch_command(
            replace(
                settings.coding_agent,
                command="codex app-server",
                repo_skill_allowlist=("commit",),
            )
        )


def test_build_launch_command_ignores_non_skill_entries_in_repo_skill_dir(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    workspace = tmp_path / "workspace"
    skills_root = workspace / ".agents" / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "README.txt").write_text("not a skill\n", encoding="utf-8")
    (skills_root / "draft").mkdir()
    create_repo_skill(workspace, "commit")

    launch_command = build_launch_command(
        replace(
            settings.coding_agent,
            command="codex app-server",
            repo_skill_allowlist=("commit",),
        ),
        str(workspace),
    )

    assert launch_command == "codex app-server"


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
    assert (
        metadata_from_message(
            session,
            {
                "method": "item/completed",
                "params": {"threadId": "thread-1", "turnId": "turn-1"},
            },
        )["thread_id"]
        == "thread-1"
    )
    assert (
        metadata_from_message(
            session,
            {
                "method": "item/completed",
                "params": {"threadId": "thread-1", "turnId": "turn-1"},
            },
        )["turn_id"]
        == "turn-1"
    )

    assert message_params({"params": {"x": 1}}) == {"x": 1}
    assert message_params({}) == {}
    assert tool_call_name({"tool": " tracker_issue_get "}) == "tracker_issue_get"
    assert tool_call_name({"name": " tracker_file_upload "}) == "tracker_file_upload"
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
async def test_message_correlation_extracts_supported_payload_shapes() -> None:
    session = make_session()
    session.current_turn_id = "turn-parent"
    assert extract_message_thread_id({"params": {"threadId": "thread-1"}}) == "thread-1"
    assert extract_message_thread_id({"threadId": "thread-2"}) == "thread-2"
    assert (
        extract_message_thread_id({"params": {"turn": {"threadId": "thread-3"}}})
        == "thread-3"
    )
    assert (
        extract_message_turn_id({"params": {"turnId": "turn-parent"}}) == "turn-parent"
    )
    assert extract_message_turn_id({"turnId": "turn-top-level"}) == "turn-top-level"
    assert extract_message_turn_id({"params": {"turn": {"id": "turn-nested"}}}) == (
        "turn-nested"
    )
    assert (
        correlate_message(
            session,
            {"method": "item/completed", "params": {"turnId": "turn-parent"}},
        ).scope
        == "current"
    )
    assert (
        correlate_message(
            session,
            {"method": "item/completed", "params": {"turnId": "turn-child"}},
        ).scope
        == "foreign"
    )
    assert (
        correlate_message(
            session,
            {"method": "item/completed", "params": {"threadId": "thread-2"}},
        ).scope
        == "foreign"
    )
    assert (
        correlate_message(
            session,
            {"method": "item/completed", "params": {"item": {"type": "note"}}},
        ).scope
        == "unattributable"
    )


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
        return {}

    async def fake_session_request(
        _session: Any, method: str, params: dict[str, Any], *, timeout_ms: int | None
    ) -> dict[str, Any]:
        sent.append({"method": method, "params": params, "timeout_ms": timeout_ms})
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.send_message",
        fake_send_message,
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.await_response",
        fake_await_response,
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.session_request",
        fake_session_request,
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
    assert (
        await start_thread(
            queue,
            process_tree,
            "/tmp",
            "never",
            "workspace-write",
            service_tier="fast",
            default_timeout_ms=100,
        )
        == "thread-1"
    )
    thread_start_calls = [
        payload for payload in sent if payload["method"] == "thread/start"
    ]
    assert "serviceTier" not in thread_start_calls[0]["params"]
    assert thread_start_calls[1]["params"]["serviceTier"] == "fast"
    session = make_session()
    assert (
        await start_turn(
            session,
            "prompt",
            make_issue(),
            output_schema=structured_turn_output_schema(("Done", "Review")),
        )
        == "turn-1"
    )
    assert sent[-1]["params"]["outputSchema"]["properties"]["decision"]["enum"] == [
        "transition",
        "blocked",
    ]
    assert sent[-1]["params"]["outputSchema"]["properties"]["next_state"]["enum"] == [
        "Done",
        "Review",
        None,
    ]


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
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                },
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

        class FailingTrackerOps:
            async def read_issue(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("bad")

        failing_executor = DynamicToolExecutor(
            FailingTrackerOps(), current_issue="ENG-1", current_project="proj-1"
        )
        assert (
            await handle_tool_call(
                session,
                on_message,
                {
                    "id": 10,
                    "params": {
                        "tool": "tracker_issue_get",
                        "arguments": {},
                    },
                },
                "{}",
                {},
                failing_executor,
            )
            == "continue"
        )
        assert events[-1] == "tool_call_failed"

        session.event_queue.put_nowait(("line", "not-json"))
        session.event_queue.put_nowait(
            (
                "line",
                json.dumps(
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "agentMessage",
                                "text": json.dumps(
                                    {
                                        "decision": "transition",
                                        "summary": "done",
                                        "next_state": "Done",
                                    }
                                ),
                            }
                        },
                    }
                ),
            )
        )
        session.event_queue.put_nowait(
            (
                "line",
                json.dumps(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            )
        )
        result = await await_turn_completion(session, on_message, executor)
        assert result.decision == "transition"
        assert result.next_state == "Done"
    finally:
        import code_factory.coding_agents.codex.app_server.turns as turns_module

        turns_module.send_message = original_send  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_client_runtime_and_observability_behaviors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings(
        tmp_path, overrides={"workspace": {"root": str(tmp_path / "workspaces")}}
    )
    issue = make_issue(identifier="ENG-1")
    client = AppServerClient(settings.coding_agent, settings.workspace)

    workspace = tmp_path / "workspaces" / "ENG-1"
    workspace.mkdir(parents=True)
    assert validate_workspace_cwd(settings.workspace.root, str(workspace)) == str(
        workspace.resolve()
    )
    with pytest.raises(AppServerError, match="invalid_workspace_cwd"):
        validate_workspace_cwd(settings.workspace.root, str(tmp_path))
    assert resolve_turn_sandbox_policy(
        settings.coding_agent,
        settings.workspace.root,
        str(workspace),
    )["writableRoots"] == [str(workspace.resolve())]

    settings_with_policy = make_settings(
        tmp_path,
        overrides={"codex": {"turn_sandbox_policy": {"type": "custom"}}},
    )
    custom_client = AppServerClient(
        settings_with_policy.coding_agent, settings_with_policy.workspace
    )
    assert resolve_turn_sandbox_policy(
        settings_with_policy.coding_agent,
        settings_with_policy.workspace.root,
        str(workspace),
    ) == {"type": "custom"}

    executor = client._build_tool_executor(str(workspace), issue)
    outcome = await executor.execute("tracker_issue_get", {})
    assert outcome.event == "tool_call_completed"
    assert outcome.success is False

    emitted: list[str] = []

    async def on_message(message: dict[str, Any]) -> None:
        emitted.append(message["event"])

    session = make_session()
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.start_turn",
        lambda session, prompt, issue, output_schema=None: asyncio.sleep(
            0, result="turn-1"
        ),
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.await_turn_completion",
        lambda session, handler, executor: asyncio.sleep(
            0,
            result=StructuredTurnResult(
                decision="transition",
                summary="done",
                next_state="Done",
            ),
        ),
    )
    result = await client.run_turn(session, "prompt", issue, on_message=on_message)
    assert result.decision == "transition"
    assert result.next_state == "Done"
    assert emitted[0] == "session_started"

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.await_turn_completion",
        lambda session, handler, executor: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="boom"):
            await client.run_turn(session, "prompt", issue, on_message=on_message)
    assert emitted[-1] == "turn_ended_with_error"
    assert "Codex turn failed" in caplog.text
    assert "session_id=thread-1-turn-1" in caplog.text
    assert "details=RuntimeError(boom)" in caplog.text

    class FakeOps:
        async def read_issue(self, issue: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "issue": {
                    "identifier": issue,
                    "title": "Fix the thing",
                    "state": {"name": "In Progress"},
                }
            }

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.runtime.build_tracker_ops",
        lambda _settings, *, allowed_roots: FakeOps(),
    )

    runtime = CodexRuntime(settings, MemoryTracker([]))
    tool_executor = runtime._build_dynamic_tool_executor(str(workspace), issue)
    outcome = await tool_executor.execute("tracker_issue_get", {})
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
        "workflow": {"version": 2, "agent": {"max_concurrent_agents": 4}},
    }
    assert state_payload(snapshot)["counts"] == {"running": 1, "retrying": 1}
    assert state_payload(snapshot)["workflow"]["version"] == 2
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


@pytest.mark.asyncio
async def test_observability_server_rebinds_only_on_effective_endpoint_change(
    tmp_path: Path,
) -> None:
    initial = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md", server={"host": "127.0.0.1", "port": 4000}
        )
    )
    server = ObservabilityHTTPServer(
        cast(Any, SimpleNamespace(snapshot=lambda: {})),
        host=initial.settings.server.host,
        port=initial.settings.server.port,
        port_override=4567,
    )
    assert server._desired_endpoint() == ("127.0.0.1", 4567)

    await server.apply_workflow_snapshot(
        replace(
            make_snapshot(
                write_workflow_file(
                    tmp_path / "UPDATED_WORKFLOW.md",
                    server={"host": "127.0.0.1", "port": 9999},
                )
            ),
            version=2,
        )
    )
    assert server._config_event.is_set() is False

    await server.apply_workflow_snapshot(
        replace(
            make_snapshot(
                write_workflow_file(
                    tmp_path / "UPDATED_HOST_WORKFLOW.md",
                    server={"host": "0.0.0.0", "port": 9999},
                )
            ),
            version=3,
        )
    )
    assert server._config_event.is_set() is True


@pytest.mark.asyncio
async def test_observability_server_disabled_until_reconfigured_and_helper_paths(
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
        cast(Any, FakeOrchestrator()), host="127.0.0.1", port=None
    )
    stop_event = asyncio.Event()

    async def fake_start_runner() -> FakeRunner:
        events.append("started")
        stop_event.set()
        return FakeRunner()

    async def fake_wait(stop: asyncio.Event, *, timeout: float | None) -> bool:
        events.append(f"wait:{timeout}")
        if timeout is None and not events.count("started"):
            await server.apply_workflow_snapshot(
                make_snapshot(
                    write_workflow_file(
                        tmp_path / "WORKFLOW.md",
                        server={"host": "127.0.0.1", "port": 4321},
                    )
                )
            )
            return False
        return stop.is_set()

    monkeypatch.setattr(server, "_start_runner", fake_start_runner)
    monkeypatch.setattr(server, "_wait_for_stop_or_config", fake_wait)
    await server.run(stop_event)
    assert events == ["wait:None", "started", "wait:None", "cleanup"]
    monkeypatch.undo()

    await server.apply_workflow_reload_error(RuntimeError("boom"))
    stop_event.set()
    assert await server._wait_for_stop_or_config(stop_event, timeout=None) is True

    stop_event.clear()
    server._config_event.set()
    assert await server._wait_for_stop_or_config(stop_event, timeout=0.001) is False
    assert server._config_event.is_set() is False

    server._config_event.set()
    assert await server._wait_for_stop_or_config(stop_event, timeout=None) is False
    assert server._config_event.is_set() is False


@pytest.mark.asyncio
async def test_observability_server_run_returns_when_retry_wait_stops() -> None:
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

    server = ObservabilityHTTPServer(
        cast(Any, FakeOrchestrator()), host="127.0.0.1", port=9999
    )
    stop_event = asyncio.Event()

    async def fake_start_runner() -> Any:
        raise OSError("boom")

    async def fake_wait(stop: asyncio.Event, *, timeout: float | None) -> bool:
        assert timeout == 5
        stop.set()
        return True

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(server, "_start_runner", fake_start_runner)
    monkeypatch.setattr(server, "_wait_for_stop_or_config", fake_wait)
    try:
        await server.run(stop_event)
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_observability_server_run_retries_loop_when_started_wait_is_not_terminal() -> (
    None
):
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
    waits = 0

    async def fake_start_runner() -> FakeRunner:
        events.append("started")
        return FakeRunner()

    async def fake_wait(stop: asyncio.Event, *, timeout: float | None) -> bool:
        nonlocal waits
        waits += 1
        if waits == 1:
            stop.set()
            return False
        return True

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(server, "_start_runner", fake_start_runner)
    monkeypatch.setattr(server, "_wait_for_stop_or_config", fake_wait)
    try:
        await server.run(stop_event)
    finally:
        monkeypatch.undo()
    assert events == ["started", "cleanup"]


@pytest.mark.asyncio
async def test_app_server_structured_output_edge_paths() -> None:
    assert parse_json_string("") is None
    assert parse_json_string("not-json") is None
    assert message_item({"params": {"item": "bad"}}) is None
    assert turn_payload({"params": {"turn": "bad"}}) == {}
    assert extract_structured_turn_result({"method": "notification"}) is None
    assert (
        structured_result_candidates(
            {"method": "item/completed", "params": {"item": "bad"}}
        )
        == []
    )
    assert (
        structured_result_candidates(
            {"method": "item/completed", "params": {"item": {"type": "note"}}}
        )
        == []
    )
    assert (
        structured_result_candidates(
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": None}},
            }
        )
        == []
    )
    assert (
        structured_result_candidates(
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "not-json"}},
            }
        )
        == []
    )
    assert (
        extract_structured_turn_result(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": '{"decision":"invalid","summary":"done"}',
                    }
                },
            }
        )
        is None
    )
    candidates = structured_result_candidates(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": '{"decision":"blocked","summary":"need input","next_state":"Todo"}',
                }
            },
        }
    )
    assert any(
        isinstance(candidate, dict) and candidate.get("decision") == "blocked"
        for candidate in candidates
    )
    parsed = extract_structured_turn_result(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": '{"decision":"transition","summary":"done","next_state":"Done"}',
                }
            },
        }
    )
    assert parsed == StructuredTurnResult(
        decision="transition",
        summary="done",
        next_state="Done",
    )


@pytest.mark.asyncio
async def test_turn_completion_reports_missing_and_terminal_status_failures() -> None:
    session = make_session()
    executor = DynamicToolExecutor(
        lambda query, variables: asyncio.sleep(0, result={"ok": True})
    )

    async def on_message(_message: dict[str, Any]) -> None:
        return None

    session.event_queue.put_nowait(
        (
            "line",
            json.dumps(
                {"method": "turn/completed", "params": {"turn": {"status": "ok"}}}
            ),
        )
    )
    with pytest.raises(AppServerError, match="invalid_turn_status"):
        await await_turn_completion(session, on_message, executor)

    session = make_session()
    session.event_queue.put_nowait(
        (
            "line",
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                }
            ),
        )
    )
    with pytest.raises(AppServerError, match="missing_structured_turn_result"):
        await await_turn_completion(session, on_message, executor)

    with pytest.raises(AppServerError, match="turn_failed"):
        await handle_turn_message(
            session,
            on_message,
            executor,
            {"method": "turn/completed", "params": {"turn": {"status": "failed"}}},
            "{}",
        )
    with pytest.raises(AppServerError, match="turn_cancelled"):
        await handle_turn_message(
            session,
            on_message,
            executor,
            {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}},
            "{}",
        )


@pytest.mark.asyncio
async def test_turn_completion_uses_turn_correlation_and_ignores_foreign_child_events() -> (
    None
):
    session = make_session()
    session.current_turn_id = "turn-parent"
    executor = DynamicToolExecutor(
        lambda query, variables: asyncio.sleep(0, result={"ok": True})
    )
    events: list[str] = []

    async def on_message(message: dict[str, Any]) -> None:
        events.append(message["event"])

    for payload in (
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-child",
                "item": {
                    "type": "agentMessage",
                    "text": json.dumps(
                        {
                            "decision": "blocked",
                            "summary": "child result",
                            "next_state": "Human Review",
                        }
                    ),
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": "turn-child",
                    "threadId": "thread-1",
                    "status": "completed",
                }
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-parent",
                "item": {
                    "type": "agentMessage",
                    "text": json.dumps(
                        {
                            "decision": "transition",
                            "summary": "parent result",
                            "next_state": "Done",
                        }
                    ),
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": "turn-parent",
                    "threadId": "thread-1",
                    "status": "completed",
                }
            },
        },
    ):
        session.event_queue.put_nowait(("line", json.dumps(payload)))

    result = await await_turn_completion(session, on_message, executor)
    assert result == StructuredTurnResult(
        decision="transition",
        summary="parent result",
        next_state="Done",
    )
    assert events.count("turn_completed") == 1
    assert events.count("notification") == 3
    assert session.current_turn_id is None


@pytest.mark.asyncio
async def test_turn_completion_fails_closed_on_ambiguous_terminal_after_foreign_turn() -> (
    None
):
    session = make_session()
    session.current_turn_id = "turn-parent"
    executor = DynamicToolExecutor(
        lambda query, variables: asyncio.sleep(0, result={"ok": True})
    )

    async def on_message(_message: dict[str, Any]) -> None:
        return None

    session.event_queue.put_nowait(
        (
            "line",
            json.dumps(
                {
                    "method": "item/updated",
                    "params": {"threadId": "thread-1", "turnId": "turn-child"},
                }
            ),
        )
    )
    session.event_queue.put_nowait(
        (
            "line",
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                }
            ),
        )
    )

    with pytest.raises(AppServerError, match="ambiguous_turn_routing"):
        await await_turn_completion(session, on_message, executor)


@pytest.mark.asyncio
async def test_turn_completion_fails_closed_on_ambiguous_structured_output_after_foreign_turn() -> (
    None
):
    session = make_session()
    session.current_turn_id = "turn-parent"
    executor = DynamicToolExecutor(
        lambda query, variables: asyncio.sleep(0, result={"ok": True})
    )

    async def on_message(_message: dict[str, Any]) -> None:
        return None

    session.event_queue.put_nowait(
        (
            "line",
            json.dumps(
                {
                    "method": "item/updated",
                    "params": {"threadId": "thread-1", "turnId": "turn-child"},
                }
            ),
        )
    )
    session.event_queue.put_nowait(
        (
            "line",
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "text": json.dumps(
                                {
                                    "decision": "transition",
                                    "summary": "missing IDs",
                                    "next_state": "Done",
                                }
                            ),
                        }
                    },
                }
            ),
        )
    )

    with pytest.raises(AppServerError, match="ambiguous_turn_routing"):
        await await_turn_completion(session, on_message, executor)


@pytest.mark.asyncio
async def test_handle_turn_message_rejects_ambiguous_terminal_failures_and_cancellations() -> (
    None
):
    session = make_session()
    executor = DynamicToolExecutor(
        lambda query, variables: asyncio.sleep(0, result={"ok": True})
    )
    events: list[str] = []

    async def on_message(message: dict[str, Any]) -> None:
        events.append(message["event"])

    assert (
        await handle_turn_message(
            session,
            on_message,
            executor,
            {"method": "turn/failed", "params": {"reason": "child failure"}},
            "{}",
            turn_scope="foreign",
        )
        == "continue"
    )
    assert events[-1] == "notification"

    with pytest.raises(AppServerError, match="ambiguous_turn_routing"):
        await handle_turn_message(
            session,
            on_message,
            executor,
            {"method": "turn/failed", "params": {"reason": "unknown failure"}},
            "{}",
            turn_scope="unattributable",
            foreign_turn_seen=True,
        )

    assert (
        await handle_turn_message(
            session,
            on_message,
            executor,
            {"method": "turn/cancelled", "params": {"reason": "child cancelled"}},
            "{}",
            turn_scope="foreign",
        )
        == "continue"
    )
    assert events[-1] == "notification"

    with pytest.raises(AppServerError, match="ambiguous_turn_routing"):
        await handle_turn_message(
            session,
            on_message,
            executor,
            {"method": "turn/cancelled", "params": {"reason": "unknown cancel"}},
            "{}",
            turn_scope="unattributable",
            foreign_turn_seen=True,
        )
