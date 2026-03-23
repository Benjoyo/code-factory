"""Turn-loop helpers that translate app-server traffic into runtime outcomes."""

from __future__ import annotations

import asyncio
import json

from ....errors import AppServerError
from ....structured_results import StructuredTurnResult
from ..tools import DynamicToolExecutor
from .messages import (
    emit_message,
    message_params,
    metadata_from_message,
    needs_input,
    tool_call_name,
    tool_request_user_input_approval_answers,
    tool_request_user_input_unavailable_answers,
)
from .session import AppServerSession
from .streams import log_non_json_stream_line, send_message
from .structured_output import extract_structured_turn_result, turn_payload
from .tool_response import build_tool_response


async def await_turn_completion(
    session: AppServerSession, on_message, tool_executor: DynamicToolExecutor
) -> StructuredTurnResult:
    """Read app-server events until the current turn completes or fails."""

    timeout = session.turn_timeout_ms / 1000
    structured_result: StructuredTurnResult | None = None
    while True:
        kind, payload = await asyncio.wait_for(session.event_queue.get(), timeout)
        if kind == "exit":
            raise AppServerError(("port_exit", payload))
        if kind != "line":
            continue
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            log_non_json_stream_line(payload, "turn stream")
            await emit_message(
                on_message,
                "malformed",
                {"payload": payload, "raw": payload},
                {"runtime_pid": session.runtime_pid},
            )
            continue
        candidate = extract_structured_turn_result(message)
        if candidate is not None:
            structured_result = candidate
        outcome = await handle_turn_message(
            session, on_message, tool_executor, message, payload
        )
        if outcome == "turn_completed":
            if structured_result is None:
                raise AppServerError("missing_structured_turn_result")
            return structured_result


async def handle_turn_message(
    session: AppServerSession,
    on_message,
    tool_executor: DynamicToolExecutor,
    message: dict,
    raw: str,
) -> str:
    """Handle one decoded app-server message and return the next turn state."""

    method = message.get("method")
    metadata = metadata_from_message(session, message)
    if method == "turn/completed":
        turn = turn_payload(message)
        status = turn.get("status")
        session.current_turn_id = None
        if status == "failed":
            await emit_message(
                on_message,
                "turn_failed",
                {"payload": message, "raw": raw, "details": message.get("params")},
                metadata,
            )
            raise AppServerError(("turn_failed", message.get("params")))
        if status == "interrupted":
            await emit_message(
                on_message,
                "turn_cancelled",
                {"payload": message, "raw": raw, "details": message.get("params")},
                metadata,
            )
            raise AppServerError(("turn_cancelled", message.get("params")))
        if status != "completed":
            raise AppServerError(("invalid_turn_status", status))
        await emit_message(
            on_message,
            "turn_completed",
            {"payload": message, "raw": raw, "details": message},
            metadata,
        )
        return "turn_completed"
    if method == "turn/failed":
        session.current_turn_id = None
        await emit_message(
            on_message,
            "turn_failed",
            {"payload": message, "raw": raw, "details": message.get("params")},
            metadata,
        )
        raise AppServerError(("turn_failed", message.get("params")))
    if method == "turn/cancelled":
        session.current_turn_id = None
        await emit_message(
            on_message,
            "turn_cancelled",
            {"payload": message, "raw": raw, "details": message.get("params")},
            metadata,
        )
        raise AppServerError(("turn_cancelled", message.get("params")))
    if isinstance(method, str):
        return await handle_turn_method(
            session, on_message, tool_executor, message, raw, metadata
        )
    await emit_message(
        on_message, "other_message", {"payload": message, "raw": raw}, metadata
    )
    return "continue"


async def handle_turn_method(
    session: AppServerSession,
    on_message,
    tool_executor: DynamicToolExecutor,
    payload: dict,
    raw: str,
    metadata: dict,
) -> str:
    """Route non-terminal methods to approval, tool, or notification handlers."""

    method = payload["method"]
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return await approve_or_require(
            session, on_message, payload, raw, metadata, "acceptForSession"
        )
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return await approve_or_require(
            session, on_message, payload, raw, metadata, "approved_for_session"
        )
    if method == "item/tool/requestUserInput":
        return await handle_tool_request_user_input(
            session, on_message, payload, raw, metadata
        )
    if method == "item/tool/call":
        return await handle_tool_call(
            session, on_message, payload, raw, metadata, tool_executor
        )
    if needs_input(method, payload):
        await emit_message(
            on_message,
            "turn_input_required",
            {"payload": payload, "raw": raw},
            metadata,
        )
        raise AppServerError(("turn_input_required", payload))
    await emit_message(
        on_message, "notification", {"payload": payload, "raw": raw}, metadata
    )
    return "continue"


async def handle_tool_call(
    session: AppServerSession,
    on_message,
    payload: dict,
    raw: str,
    metadata: dict,
    tool_executor: DynamicToolExecutor,
) -> str:
    """Execute a dynamic tool request and send the result back to the app server."""

    params = message_params(payload)
    outcome = await tool_executor.execute(
        tool_call_name(params), params.get("arguments", {})
    )
    event = outcome.event
    response = build_tool_response(outcome)
    if event == "tool_call_completed" and outcome.success is not True:
        event = "tool_call_failed"
    await send_message(
        session.process_tree, {"id": payload.get("id"), "result": response}
    )
    await emit_message(on_message, event, {"payload": payload, "raw": raw}, metadata)
    return "continue"


async def approve_or_require(
    session: AppServerSession,
    on_message,
    payload: dict,
    raw: str,
    metadata: dict,
    decision: str,
) -> str:
    """Auto-approve a guarded action or surface that human approval is required."""

    if not session.auto_approve_requests:
        await emit_message(
            on_message, "approval_required", {"payload": payload, "raw": raw}, metadata
        )
        raise AppServerError(("approval_required", payload))
    await send_message(
        session.process_tree,
        {"id": payload.get("id"), "result": {"decision": decision}},
    )
    await emit_message(
        on_message,
        "approval_auto_approved",
        {"payload": payload, "raw": raw, "decision": decision},
        metadata,
    )
    return "continue"


async def handle_tool_request_user_input(
    session: AppServerSession, on_message, payload: dict, raw: str, metadata: dict
) -> str:
    """Answer request-user-input calls when policy allows a non-interactive default."""

    params = message_params(payload)
    if session.auto_approve_requests:
        answers = tool_request_user_input_approval_answers(params)
        if answers is not None:
            await send_message(
                session.process_tree,
                {"id": payload.get("id"), "result": {"answers": answers}},
            )
            await emit_message(
                on_message,
                "approval_auto_approved",
                {"payload": payload, "raw": raw, "decision": "Approve this Session"},
                metadata,
            )
            return "continue"
    answers = tool_request_user_input_unavailable_answers(params)
    if answers is None:
        await emit_message(
            on_message,
            "turn_input_required",
            {"payload": payload, "raw": raw},
            metadata,
        )
        raise AppServerError(("turn_input_required", payload))
    await send_message(
        session.process_tree, {"id": payload.get("id"), "result": {"answers": answers}}
    )
    await emit_message(
        on_message,
        "tool_input_auto_answered",
        {"payload": payload, "raw": raw},
        metadata,
    )
    return "continue"
