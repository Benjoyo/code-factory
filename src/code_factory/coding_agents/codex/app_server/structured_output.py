"""Structured-output extraction helpers for app-server turn events."""

from __future__ import annotations

import json
from typing import Any

from ....structured_results import (
    StructuredTurnResult,
    normalize_structured_turn_result,
)
from .messages import message_params


def extract_structured_turn_result(
    message: dict[str, Any],
) -> StructuredTurnResult | None:
    """Return a structured turn result from the documented agent-message path."""

    for candidate in structured_result_candidates(message):
        result = normalize_structured_turn_result(candidate)
        if result is not None:
            return result
    return None


def structured_result_candidates(message: dict[str, Any]) -> list[Any]:
    """Collect documented structured-result payloads from one app-server event."""

    if message.get("method") != "item/completed":
        return []
    item = message_item(message)
    if not isinstance(item, dict) or item.get("type") != "agentMessage":
        return []
    text = item.get("text")
    if not isinstance(text, str):
        return []
    parsed = parse_json_string(text)
    return [parsed] if parsed is not None else []


def turn_payload(message: dict[str, Any]) -> dict[str, Any]:
    """Return the nested turn payload for a turn event."""

    turn = message_params(message).get("turn")
    return turn if isinstance(turn, dict) else {}


def message_item(message: dict[str, Any]) -> dict[str, Any] | None:
    """Return the nested item payload for an item event."""

    item = message_params(message).get("item")
    return item if isinstance(item, dict) else None


def parse_json_string(value: str) -> Any:
    """Best-effort JSON decoding for agent text payloads."""

    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
