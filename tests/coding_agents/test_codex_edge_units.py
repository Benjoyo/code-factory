from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError, model_validator

from code_factory.coding_agents.codex.app_server.client import AppServerClient
from code_factory.coding_agents.codex.app_server.messages import (
    approval_option_label,
    message_summary,
    metadata_from_message,
    rate_limits_from_payload,
    tool_request_user_input_approval_answers,
    tool_request_user_input_unavailable_answers,
)
from code_factory.coding_agents.codex.app_server.policies import (
    resolve_turn_sandbox_policy,
    review_turn_sandbox_policy,
)
from code_factory.coding_agents.codex.app_server.protocol import (
    await_response,
    start_thread,
    start_turn,
)
from code_factory.coding_agents.codex.app_server.reviews import (
    await_review_completion,
    extract_review_output,
)
from code_factory.coding_agents.codex.app_server.session import AppServerSession
from code_factory.coding_agents.codex.app_server.tool_response import (
    build_tool_response,
    encode_payload,
)
from code_factory.coding_agents.codex.app_server.turns import (
    await_turn_completion,
    handle_tool_call,
    handle_tool_request_user_input,
    handle_turn_message,
)
from code_factory.coding_agents.codex.config import (
    parse_coding_agent_settings,
    validate_coding_agent_settings,
)
from code_factory.coding_agents.codex.runtime import CodexRuntime
from code_factory.coding_agents.codex.tools import DynamicToolExecutor
from code_factory.coding_agents.codex.tools.registry import (
    _validation_error_payload,
    build_input_schema,
    dynamic_tool,
    unexpected_tool_failure_payload,
)
from code_factory.coding_agents.codex.tools.results import (
    ToolExecutionOutcome,
    ToolResult,
)
from code_factory.coding_agents.codex.tools.tracker.attachment_tools import (
    TrackerFileUploadInput,
    TrackerPrLinkInput,
)
from code_factory.coding_agents.codex.tools.tracker.comment_tools import (
    TrackerCommentCreateInput,
    TrackerCommentUpdateInput,
)
from code_factory.coding_agents.codex.tools.tracker.issue_read import (
    TrackerIssueGetInput,
    TrackerIssueSearchInput,
    TrackerStatesInput,
)
from code_factory.coding_agents.codex.tools.tracker.issue_write import (
    TrackerIssueCreateInput,
    TrackerIssueUpdateInput,
)
from code_factory.coding_agents.codex.tools.tracker.linear_errors import (
    linear_error_payload,
)
from code_factory.errors import AppServerError, TrackerClientError, WorkflowLoadError
from code_factory.prompts import build_prompt
from code_factory.prompts.review_assets import review_output_schema
from code_factory.trackers.memory import MemoryTracker
from code_factory.workflow.loader import load_workflow

from ..conftest import make_issue, make_snapshot, write_workflow_file


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
        event_queue=asyncio.Queue(),
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
    assert TrackerIssueGetInput.model_validate(
        {"issue": "ENG-1"}
    ) == TrackerIssueGetInput(
        issue="ENG-1",
        include_comments=False,
        include_attachments=False,
    )
    assert TrackerIssueSearchInput.model_validate(
        {"limit": 5}
    ) == TrackerIssueSearchInput(
        query=None,
        state=None,
        limit=5,
    )
    assert TrackerStatesInput.model_validate({}) == TrackerStatesInput(issue=None)
    with pytest.raises(ValidationError) as excinfo:
        TrackerIssueGetInput.model_validate({"issue": "ENG-1", "extra": True})
    assert _validation_error_payload("tracker_issue_get", excinfo.value) == {
        "error": {"message": "tracker_issue_get: unexpected field: `extra`"}
    }

    assert TrackerIssueCreateInput.model_validate(
        {"title": "Follow-up"}
    ) == TrackerIssueCreateInput(title="Follow-up")
    with pytest.raises(
        ValidationError, match="String should have at least 1 character"
    ):
        TrackerIssueCreateInput.model_validate({"title": ""})
    with pytest.raises(
        ValidationError, match="at least one update field is required"
    ) as excinfo:
        TrackerIssueUpdateInput.model_validate({})
    assert _validation_error_payload("tracker_issue_update", excinfo.value) == {
        "error": {
            "message": "tracker_issue_update: at least one update field is required"
        }
    }
    assert TrackerIssueUpdateInput.model_validate(
        {"description": "Updated"}
    ) == TrackerIssueUpdateInput(description="Updated")
    with pytest.raises(ValidationError, match="Field required"):
        TrackerCommentCreateInput.model_validate({"issue": "ENG-1"})
    assert TrackerCommentUpdateInput.model_validate(
        {"comment_id": "c1", "body": "hello"}
    ) == TrackerCommentUpdateInput(comment_id="c1", body="hello")
    assert TrackerPrLinkInput.model_validate({"url": "https://example.com"}) == (
        TrackerPrLinkInput(url="https://example.com")
    )
    with pytest.raises(ValidationError, match="Field required"):
        TrackerFileUploadInput.model_validate({})

    assert json.loads(encode_payload({"a": 1})) == {"a": 1}
    assert encode_payload("raw") == "'raw'"

    assert linear_error_payload(TrackerClientError("missing_linear_api_token")) == {
        "error": {
            "message": "Code Factory is missing Linear auth. Set `linear.api_key` in `WORKFLOW.md` or export `LINEAR_API_KEY`."
        }
    }
    assert linear_error_payload(TrackerClientError("missing_linear_project")) == {
        "error": {
            "message": "Code Factory is missing the default tracker project. Set `tracker.project` in `WORKFLOW.md`."
        }
    }
    assert linear_error_payload(TrackerClientError(("linear_api_status", 503))) == {
        "error": {
            "message": "Tracker request failed with HTTP 503.",
            "status": 503,
        }
    }
    assert linear_error_payload(
        TrackerClientError(("linear_api_request", "timeout"))
    ) == {
        "error": {
            "message": "Tracker request failed before receiving a successful response.",
            "reason": "'timeout'",
        }
    }
    assert linear_error_payload(
        TrackerClientError(("tracker_missing_field", "`issue` is required"))
    ) == {"error": {"message": "`issue` is required"}}
    assert linear_error_payload(TrackerClientError(("tracker_not_found", "ENG-1"))) == {
        "error": {"message": "Tracker record not found: ENG-1"}
    }
    assert linear_error_payload(TypeError("invalid_arguments")) == {
        "error": {
            "message": "Tracker operation failed.",
            "reason": "'invalid_arguments'",
        }
    }
    assert linear_error_payload(ValueError(("other", "x"))) == {
        "error": {
            "message": "Tracker operation failed.",
            "reason": "\"('other', 'x')\"",
        }
    }
    assert linear_error_payload(RuntimeError("boom")) == {
        "error": {
            "message": "Tracker operation failed.",
            "reason": "'boom'",
        }
    }

    class FailingTrackerOps:
        async def read_issue(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

    sync_outcome = await DynamicToolExecutor(
        FailingTrackerOps(),
        allowed_roots=(str(tmp_path),),
        current_issue="ENG-1",
        current_project="proj-1",
    ).execute("tracker_issue_get", {})
    assert sync_outcome.success is False
    assert sync_outcome.payload == {
        "error": {
            "message": "Tracker operation failed.",
            "reason": "'boom'",
        }
    }

    class SimpleInput(BaseModel):
        value: int

    async def sample_handler(_context, parsed):
        return ToolResult.ok({"value": parsed.value})

    sample_tool = dynamic_tool(
        name="sample",
        description="sample",
        args_model=SimpleInput,
    )(sample_handler)
    parsed = sample_tool.parse({"value": "7"})
    assert parsed.value == 7
    assert ToolResult.fail({"bad": True}) == ToolResult(
        success=False, payload={"bad": True}
    )

    class FlexibleInput(BaseModel):
        value: str | int | None

    schema = build_input_schema(FlexibleInput)
    assert schema["properties"]["value"]["anyOf"] == [
        {"type": "string"},
        {"type": "integer"},
        {"type": "null"},
    ]

    class NullableObjectInput(BaseModel):
        value: dict[str, Any] | None

    schema = build_input_schema(NullableObjectInput)
    assert schema["properties"]["value"] == {
        "type": ["object", "null"],
        "additionalProperties": True,
    }

    with pytest.raises(ValidationError) as excinfo:
        SimpleInput.model_validate({})
    assert _validation_error_payload("sample", excinfo.value) == {
        "error": {"message": "sample: `value` is required"}
    }

    class ValueErrorInput(BaseModel):
        value: int

        @model_validator(mode="after")
        def explode(self) -> ValueErrorInput:
            raise ValueError("boom")

    with pytest.raises(ValidationError) as excinfo:
        ValueErrorInput.model_validate({"value": 1})
    assert _validation_error_payload("sample", excinfo.value) == {
        "error": {"message": "sample: boom"}
    }

    assert _validation_error_payload("sample", RuntimeError("boom")) == {
        "error": {
            "message": "`sample` received invalid input.",
            "reason": "boom",
        }
    }
    assert unexpected_tool_failure_payload("sample") == {
        "error": {"message": "Dynamic tool `sample` failed unexpectedly."}
    }

    with pytest.raises(ValueError, match="non-empty tool name"):
        dynamic_tool(
            name=" ",
            args_model=SimpleInput,
        )(sample_handler)

    async def undocumented_handler(_context, _parsed):
        return ToolResult.ok({})

    undocumented_handler.__doc__ = " "
    with pytest.raises(ValueError, match="requires a description or docstring"):
        dynamic_tool(
            args_model=SimpleInput,
        )(undocumented_handler)

    @dynamic_tool(args_model=SimpleInput, name="explode", description="explode")
    async def exploding_handler(_context, _parsed) -> ToolResult:
        raise RuntimeError("boom")

    class FakeTrackerOps:
        async def read_issue(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"issue": {"identifier": "ENG-1", "state": {"name": "Todo"}}}

    outcome = await DynamicToolExecutor(
        FakeTrackerOps(), tools=(cast(Any, exploding_handler),)
    ).execute("explode", {"value": 1})
    assert outcome.success is False
    assert outcome.payload == {
        "error": {"message": "Dynamic tool `explode` failed unexpectedly."}
    }


@pytest.mark.asyncio
async def test_turn_handlers_cover_stream_and_input_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingExecutor:
        async def execute(
            self, _tool: str | None, _arguments: Any
        ) -> ToolExecutionOutcome:
            return ToolExecutionOutcome(
                success=False,
                payload={"error": {"message": "bad"}},
                event="tool_call_completed",
            )

    class SuccessExecutor:
        async def execute(
            self, _tool: str | None, _arguments: Any
        ) -> ToolExecutionOutcome:
            return ToolExecutionOutcome(
                success=True,
                payload={"ok": True},
                event="tool_call_completed",
            )

    sent_messages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.turns.send_message",
        lambda _process_tree, payload: sent_messages.append(payload) or _async_noop(),
    )

    session = make_session()
    messages: list[dict[str, Any]] = []
    await session.event_queue.put(("stderr", "ignored"))
    await session.event_queue.put(("line", "not-json"))
    await session.event_queue.put(
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
    await session.event_queue.put(
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
    result = await await_turn_completion(
        session,
        lambda message: collect_messages(message, messages),
        cast(Any, FailingExecutor()),
    )
    assert result.decision == "transition"
    assert result.next_state == "Done"
    assert [message["event"] for message in messages] == [
        "malformed",
        "notification",
        "turn_completed",
    ]

    exit_session = make_session()
    await exit_session.event_queue.put(("exit", 9))
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
        {
            "id": 2,
            "params": {
                "tool": "tracker_issue_get",
                "arguments": {},
            },
        },
        "{}",
        {"runtime_pid": "123"},
        cast(Any, FailingExecutor()),
    )
    assert sent_messages[-1] == {
        "id": 2,
        "result": build_tool_response(
            ToolExecutionOutcome(
                success=False,
                payload={"error": {"message": "bad"}},
                event="tool_call_completed",
            )
        ),
    }
    assert messages[-1]["event"] == "tool_call_failed"

    messages = []
    await handle_tool_call(
        session,
        lambda message: collect_messages(message, messages),
        {
            "id": 12,
            "params": {
                "tool": "tracker_issue_get",
                "arguments": {},
            },
        },
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
async def test_session_stop_unblocks_waiting_turn_and_pending_requests() -> None:
    session = make_session()
    pending = asyncio.get_running_loop().create_future()
    done_pending = asyncio.get_running_loop().create_future()
    done_pending.set_result({"ok": True})
    session.pending_requests[7] = pending
    session.pending_requests[8] = done_pending

    wait_task = asyncio.create_task(
        await_turn_completion(
            session,
            lambda message: collect_messages(message, []),
            cast(Any, object()),
        )
    )
    await asyncio.sleep(0)
    await session.stop()

    with pytest.raises(AppServerError, match="port_exit"):
        await wait_task
    with pytest.raises(AppServerError, match="port_exit"):
        await pending
    assert done_pending.result() == {"ok": True}


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

    session = make_session()
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.session_request",
        lambda *_args, **_kwargs: asyncio.sleep(0, result={"turn": {}}),
    )
    with pytest.raises(AppServerError, match="invalid_turn_payload"):
        await start_turn(session, "prompt", make_issue())

    settings = make_settings(tmp_path)
    client = AppServerClient(settings.coding_agent, settings.workspace)

    class BootstrapProcessTree:
        def __init__(self) -> None:
            self.process = FakeProcess()
            self.terminated = 0

        async def terminate(self) -> None:
            self.terminated += 1

    async def sleeping_reader(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.stdout_reader",
        sleeping_reader,
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.stderr_reader",
        sleeping_reader,
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.wait_for_exit",
        sleeping_reader,
    )

    async def fail_initialize(*_args: Any, **_kwargs: Any) -> None:
        raise AppServerError("boom")

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.send_initialize",
        fail_initialize,
    )

    process_tree = BootstrapProcessTree()
    with pytest.raises(AppServerError, match="boom"):
        await client._bootstrap_session(cast(Any, process_tree), "/tmp/workspace")
    assert process_tree.terminated == 1

    fallback_executor = client._build_tool_executor(
        "/tmp/workspace", make_issue(identifier="ENG-1")
    )
    outcome = await fallback_executor.execute("tracker_issue_get", {})
    assert outcome.event == "tool_call_completed"
    assert outcome.success is False
    assert outcome.payload["error"]["message"] == "Tracker operation failed."

    explicit_settings = make_settings(
        tmp_path / "explicit",
        overrides={"codex": {"turn_sandbox_policy": {"type": "dangerouslyBypass"}}},
    )
    assert resolve_turn_sandbox_policy(
        explicit_settings.coding_agent,
        explicit_settings.workspace.root,
        "/tmp/workspace",
    ) == {"type": "dangerouslyBypass"}


@pytest.mark.asyncio
async def test_review_turn_protocol_helpers() -> None:
    session = make_session()
    calls: list[tuple[str, dict[str, Any], int | None]] = []

    async def fake_session_request(
        _session: Any,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int | None,
    ) -> dict[str, Any]:
        calls.append((method, params, timeout_ms))
        return {"turn": {"id": "turn-review-1"}}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.protocol.session_request",
        fake_session_request,
    )
    try:
        turn_id = await start_turn(
            session,
            "Review",
            make_issue(identifier="ENG-1"),
            output_schema=review_output_schema(),
            sandbox_policy=review_turn_sandbox_policy(
                "/tmp/workspace", "/tmp/workspace"
            ),
        )
    finally:
        monkeypatch.undo()

    assert turn_id == "turn-review-1"
    assert calls == [
        (
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "Review"}],
                "cwd": "/tmp/workspace",
                "title": "ENG-1: Test issue",
                "approvalPolicy": "never",
                "sandboxPolicy": review_turn_sandbox_policy(
                    "/tmp/workspace", "/tmp/workspace"
                ),
                "outputSchema": review_output_schema(),
            },
            50,
        )
    ]


@pytest.mark.asyncio
async def test_review_completion_extracts_structured_output() -> None:
    session = make_session()
    session.current_turn_id = "turn-review-1"
    payload = {
        "method": "item/completed",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-review-1",
            "item": {
                "id": "message-1",
                "type": "agentMessage",
                "text": json.dumps(
                    {
                        "findings": [
                            {
                                "title": "Broken guard",
                                "body": "Null access can crash this path.",
                                "confidence_score": 0.9,
                                "priority": 1,
                                "code_location": {
                                    "absolute_file_path": "/tmp/workspace/app.py",
                                    "line_range": {"start": 11, "end": 12},
                                },
                            }
                        ],
                        "overall_correctness": "patch is incorrect",
                        "overall_explanation": "A correctness issue remains.",
                        "overall_confidence_score": 0.88,
                    }
                ),
            },
        },
    }
    await session.event_queue.put(("line", json.dumps(payload)))
    await session.event_queue.put(
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

    review_output = await await_review_completion(
        session,
        lambda _message: asyncio.sleep(0),
        cast(Any, object()),
    )

    assert review_output.overall_correctness == "patch is incorrect"
    assert review_output.findings[0].title == "Broken guard"
    assert review_output.findings[0].code_location.absolute_file_path.endswith("app.py")
    assert extract_review_output(payload) == review_output


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
    parsed = parse_coding_agent_settings(
        {
            "codex": {
                "model": "gpt-5.3-codex",
                "reasoning_effort": "high",
                "fast_mode": True,
            }
        }
    )
    assert parsed.model == "gpt-5.3-codex"
    assert parsed.reasoning_effort == "high"
    assert parsed.fast_mode is True
    with pytest.raises(Exception, match="codex.model can't be blank"):
        parse_coding_agent_settings({"codex": {"model": "  "}})
    with pytest.raises(Exception, match="codex.fast_mode must be a boolean"):
        parse_coding_agent_settings({"codex": {"fast_mode": "yes"}})
    invalid_shell_command = replace(
        settings,
        coding_agent=replace(
            settings.coding_agent,
            command='codex "app-server',
            model="gpt-5.3-codex",
        ),
    )
    with pytest.raises(Exception, match="must be a valid shell-style command"):
        validate_coding_agent_settings(invalid_shell_command)
    invalid_model_command = replace(
        settings,
        coding_agent=replace(
            settings.coding_agent,
            command="codex",
            model="gpt-5.3-codex",
        ),
    )
    with pytest.raises(Exception, match="must include an `app-server` argument"):
        validate_coding_agent_settings(invalid_model_command)


@pytest.mark.asyncio
async def test_codex_runtime_dynamic_tool_executor_uses_shared_tracker_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class FakeOps:
        async def read_issue(self, issue: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(((issue,), kwargs))
            return {
                "issue": {
                    "identifier": issue,
                    "title": "Fix the thing",
                    "state": {"name": "In Progress"},
                }
            }

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.runtime.build_tracker_ops",
        lambda settings, *, allowed_roots: FakeOps(),
    )

    runtime = CodexRuntime(settings, cast(Any, MemoryTracker([])))
    tool_executor = runtime._build_dynamic_tool_executor(
        "/tmp/workspace", make_issue(identifier="ENG-1")
    )
    outcome = await tool_executor.execute("tracker_issue_get", {})
    assert outcome.success is True
    assert calls == [
        (
            ("ENG-1",),
            {
                "include_description": True,
                "include_comments": False,
                "include_attachments": False,
                "include_relations": True,
            },
        )
    ]
    with pytest.raises(TypeError, match="Unsupported session type"):
        await runtime.run_turn(object(), "prompt", make_issue())  # type: ignore[arg-type]
