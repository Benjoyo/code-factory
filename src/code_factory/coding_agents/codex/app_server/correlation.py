"""Helpers for correlating streamed app-server messages to the active turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .session import AppServerSession

TurnScope = Literal["current", "foreign", "unattributable"]


@dataclass(slots=True, frozen=True)
class MessageCorrelation:
    """Correlation details extracted from one streamed app-server message."""

    thread_id: str | None
    turn_id: str | None
    scope: TurnScope


def correlate_message(
    session: AppServerSession, message: dict[str, Any]
) -> MessageCorrelation:
    """Classify one streamed message relative to the active session turn."""

    thread_id = extract_message_thread_id(message)
    turn_id = extract_message_turn_id(message)
    if isinstance(thread_id, str) and thread_id != session.thread_id:
        return MessageCorrelation(thread_id=thread_id, turn_id=turn_id, scope="foreign")
    current_turn_id = session.current_turn_id
    if isinstance(turn_id, str):
        if current_turn_id is None or turn_id != current_turn_id:
            return MessageCorrelation(
                thread_id=thread_id,
                turn_id=turn_id,
                scope="foreign",
            )
        return MessageCorrelation(thread_id=thread_id, turn_id=turn_id, scope="current")
    return MessageCorrelation(
        thread_id=thread_id,
        turn_id=turn_id,
        scope="unattributable",
    )


def extract_message_thread_id(message: dict[str, Any]) -> str | None:
    """Return the streamed message thread identifier when present."""

    return _first_string(
        message,
        (
            ("params", "threadId"),
            ("threadId",),
            ("params", "turn", "threadId"),
            ("turn", "threadId"),
        ),
    )


def extract_message_turn_id(message: dict[str, Any]) -> str | None:
    """Return the streamed message turn identifier when present."""

    return _first_string(
        message,
        (
            ("params", "turnId"),
            ("turnId",),
            ("params", "turn", "id"),
            ("turn", "id"),
        ),
    )


def ambiguous_turn_routing_reason(
    session: AppServerSession, message: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Build a stable protocol failure payload for ambiguous multiplexed traffic."""

    correlation = correlate_message(session, message)
    return (
        "ambiguous_turn_routing",
        {
            "method": message.get("method"),
            "active_thread_id": session.thread_id,
            "active_turn_id": session.current_turn_id,
            "message_thread_id": correlation.thread_id,
            "message_turn_id": correlation.turn_id,
            "params": message.get("params"),
        },
    )


def _first_string(payload: Any, paths: tuple[tuple[str, ...], ...]) -> str | None:
    for path in paths:
        value = _value_at_path(payload, path)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _value_at_path(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current
