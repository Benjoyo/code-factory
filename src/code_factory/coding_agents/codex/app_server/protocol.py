from __future__ import annotations

"""Protocol helpers for driving the Codex app-server JSON-RPC stream."""

import asyncio
import json
from typing import Any

from ....errors import AppServerError
from ....issues import Issue
from ..tools import tool_specs
from .session import AppServerSession
from .streams import log_non_json_stream_line, send_message

INITIALIZE_ID = 1
THREAD_START_ID = 2
TURN_START_ID = 3


async def send_initialize(
    stdout_queue: asyncio.Queue[tuple[str, Any]],
    process_tree,
    *,
    default_timeout_ms: int,
) -> None:
    await send_message(
        process_tree,
        {
            "method": "initialize",
            "id": INITIALIZE_ID,
            "params": {
                "capabilities": {"experimentalApi": True},
                "clientInfo": {
                    "name": "code-factory-orchestrator",
                    "title": "Code Factory Orchestrator",
                    "version": "0.1.0",
                },
            },
        },
    )
    await await_response(
        stdout_queue,
        INITIALIZE_ID,
        timeout_ms=None,
        default_timeout_ms=default_timeout_ms,
    )
    await send_message(process_tree, {"method": "initialized", "params": {}})


async def start_thread(
    stdout_queue: asyncio.Queue[tuple[str, Any]],
    process_tree,
    workspace: str,
    approval_policy: str | dict[str, Any],
    thread_sandbox: str,
    *,
    default_timeout_ms: int,
) -> str:
    await send_message(
        process_tree,
        {
            "method": "thread/start",
            "id": THREAD_START_ID,
            "params": {
                "approvalPolicy": approval_policy,
                "sandbox": thread_sandbox,
                "cwd": workspace,
                "dynamicTools": tool_specs(),
            },
        },
    )
    result = await await_response(
        stdout_queue,
        THREAD_START_ID,
        timeout_ms=None,
        default_timeout_ms=default_timeout_ms,
    )
    thread = result.get("thread")
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        raise AppServerError(("invalid_thread_payload", result))
    return thread["id"]


async def start_turn(
    session: AppServerSession,
    prompt: str,
    issue: Issue,
    *,
    output_schema: dict[str, Any] | None = None,
) -> str:
    params = {
        "threadId": session.thread_id,
        "input": [{"type": "text", "text": prompt}],
        "cwd": session.workspace,
        "title": f"{issue.identifier}: {issue.title}",
        "approvalPolicy": session.approval_policy,
        "sandboxPolicy": session.turn_sandbox_policy,
    }
    if output_schema is not None:
        params["outputSchema"] = output_schema
    await send_message(
        session.process_tree,
        {
            "method": "turn/start",
            "id": TURN_START_ID,
            "params": params,
        },
    )
    result = await await_response(
        session.stdout_queue,
        TURN_START_ID,
        timeout_ms=session.read_timeout_ms,
        default_timeout_ms=session.read_timeout_ms,
    )
    turn = result.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise AppServerError(("invalid_turn_payload", result))
    return turn["id"]


async def await_response(
    stdout_queue: asyncio.Queue[tuple[str, Any]],
    request_id: int,
    *,
    timeout_ms: int | None,
    default_timeout_ms: int,
) -> dict[str, Any]:
    """Wait for the request with `request_id` to complete, raising on errors."""
    timeout = (timeout_ms or default_timeout_ms) / 1000
    while True:
        kind, payload = await asyncio.wait_for(stdout_queue.get(), timeout)
        if kind == "exit":
            raise AppServerError(("port_exit", payload))
        if kind != "line":
            continue
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            log_non_json_stream_line(payload, "response stream")
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise AppServerError(("response_error", message["error"]))
        result = message.get("result")
        if not isinstance(result, dict):
            raise AppServerError(("response_error", message))
        return result
