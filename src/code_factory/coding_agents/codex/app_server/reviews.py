"""Review-loop helpers for schema-driven app-server review turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ....errors import AppServerError
from ...review_models import ReviewOutput, normalize_review_output
from ..tools.results import ToolExecutionOutcome
from .messages import emit_message
from .structured_output import message_item, parse_json_string
from .turns import handle_turn_message


async def await_review_completion(
    session,
    on_message,
    tool_executor,
) -> ReviewOutput:
    """Read normal turn events until a review turn produces structured output."""

    timeout = session.turn_timeout_ms / 1000
    review_output: ReviewOutput | None = None
    while True:
        kind, payload = await asyncio.wait_for(session.event_queue.get(), timeout)
        if kind == "exit":
            raise AppServerError(("port_exit", payload))
        if kind != "line":
            continue
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            await emit_message(
                on_message,
                "malformed",
                {"payload": payload, "raw": payload},
                {"runtime_pid": session.runtime_pid},
            )
            continue
        candidate = extract_review_output(message)
        if candidate is not None:
            review_output = candidate
        if (
            await handle_turn_message(
                session,
                on_message,
                tool_executor,
                message,
                payload,
            )
            == "turn_completed"
        ):
            if review_output is None:
                raise AppServerError("missing_review_output")
            return review_output


def extract_review_output(message: dict[str, Any]) -> ReviewOutput | None:
    """Collect review output from normal-turn agent messages."""

    item = message_item(message)
    if not isinstance(item, dict) or item.get("type") != "agentMessage":
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    return normalize_review_output(parse_json_string(text))


class DisabledDynamicToolExecutor:
    async def execute(self, *_args, **_kwargs) -> ToolExecutionOutcome:
        return ToolExecutionOutcome(
            success=False,
            event="unsupported_tool_call",
            payload={
                "error": {
                    "message": "Dynamic tools are disabled for review turns.",
                }
            },
        )
