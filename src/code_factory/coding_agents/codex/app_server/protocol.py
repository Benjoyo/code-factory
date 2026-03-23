from __future__ import annotations

"""Protocol helpers for driving the Codex app-server JSON-RPC stream."""

import asyncio
from typing import Any

from ....errors import AppServerError
from ....issues import Issue
from ..tools import tool_specs
from .session import AppServerSession
from .streams import send_message

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
    result = await session_request(
        session,
        "turn/start",
        params,
        timeout_ms=session.read_timeout_ms,
    )
    turn = result.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise AppServerError(("invalid_turn_payload", result))
    session.current_turn_id = turn["id"]
    return turn["id"]


async def steer_turn(session: AppServerSession, message: str) -> str:
    """Append steering input to the currently active turn."""

    if not isinstance(session.current_turn_id, str):
        raise AppServerError("no_active_turn")
    result = await session_request(
        session,
        "turn/steer",
        {
            "threadId": session.thread_id,
            "input": [{"type": "text", "text": message}],
            "expectedTurnId": session.current_turn_id,
        },
        timeout_ms=session.read_timeout_ms,
    )
    turn_id = result.get("turnId")
    if not isinstance(turn_id, str):
        raise AppServerError(("invalid_turn_steer_payload", result))
    return turn_id


async def session_request(
    session: AppServerSession,
    method: str,
    params: dict[str, Any],
    *,
    timeout_ms: int | None,
) -> dict[str, Any]:
    """Send a session-bound request and wait for the routed response."""

    request_id = session.reserve_request_id()
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    session.pending_requests[request_id] = future
    try:
        async with session.write_lock:
            await send_message(
                session.process_tree,
                {"method": method, "id": request_id, "params": params},
            )
        timeout = (timeout_ms or session.read_timeout_ms) / 1000
        return await asyncio.wait_for(future, timeout)
    finally:
        session.pending_requests.pop(request_id, None)


async def await_response(
    stdout_queue: asyncio.Queue[tuple[str, Any]],
    request_id: int,
    *,
    timeout_ms: int | None,
    default_timeout_ms: int,
) -> dict[str, Any]:
    """Wait for the bootstrap request with `request_id` to complete."""

    timeout = (timeout_ms or default_timeout_ms) / 1000
    while True:
        kind, payload = await asyncio.wait_for(stdout_queue.get(), timeout)
        if kind == "exit":
            raise AppServerError(("port_exit", payload))
        if kind != "line":
            continue
        import json

        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise AppServerError(("response_error", message["error"]))
        result = message.get("result")
        if not isinstance(result, dict):
            raise AppServerError(("response_error", message))
        return result
