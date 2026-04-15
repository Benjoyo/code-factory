from __future__ import annotations

"""Utility functions for interpreting Codex App Server events and responses."""

from datetime import UTC, datetime
from typing import Any

from ...base import AgentMessageHandler
from .correlation import extract_message_thread_id, extract_message_turn_id
from .session import AppServerSession

NON_INTERACTIVE_TOOL_INPUT_ANSWER = (
    "This is a non-interactive session. Operator input is unavailable."
)


async def emit_message(
    on_message: AgentMessageHandler,
    event: str,
    details: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    await on_message(
        {**metadata, **details, "event": event, "timestamp": datetime.now(UTC)}
    )


async def default_on_message(_message: dict[str, Any]) -> None:
    return None


def metadata_from_message(
    session: AppServerSession, payload: dict[str, Any]
) -> dict[str, Any]:
    """Summarize payload details for observability consumers."""
    metadata: dict[str, Any] = {}
    if session.runtime_pid is not None:
        metadata["runtime_pid"] = session.runtime_pid
    thread_id = extract_message_thread_id(payload)
    turn_id = extract_message_turn_id(payload)
    if isinstance(thread_id, str):
        metadata["thread_id"] = thread_id
    if isinstance(turn_id, str):
        metadata["turn_id"] = turn_id
    token_usage = extract_token_usage(payload)
    if token_usage:
        metadata["token_usage"] = token_usage
    rate_limits = extract_rate_limits(payload)
    if rate_limits is not None:
        metadata["rate_limits"] = rate_limits
    summary = message_summary(payload)
    if summary is not None:
        metadata["message_summary"] = summary
    return metadata


def message_params(payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params")
    return params if isinstance(params, dict) else {}


def tool_call_name(params: dict[str, Any]) -> str | None:
    raw = params.get("tool") or params.get("name")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def needs_input(method: str, payload: dict[str, Any]) -> bool:
    """Detect whether the runtime has asked an operator for additional input."""
    if method in {
        "turn/input_required",
        "turn/needs_input",
        "turn/need_input",
        "turn/request_input",
        "turn/request_response",
        "turn/provide_input",
        "turn/approval_required",
    }:
        return True
    params = message_params(payload)
    return any(
        source.get(field) is True
        or source.get(field) in {"input_required", "needs_input"}
        for source in (payload, params)
        for field in (
            "requiresInput",
            "needsInput",
            "input_required",
            "inputRequired",
            "type",
        )
        if isinstance(source, dict)
    )


def tool_request_user_input_approval_answers(
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the default approval answers when the agent requests operator confirmation."""
    questions = params.get("questions")
    if not isinstance(questions, list):
        return None
    answers: dict[str, Any] = {}
    for question in questions:
        if not isinstance(question, dict):
            return None
        question_id = question.get("id")
        options = question.get("options")
        if not isinstance(question_id, str) or not isinstance(options, list):
            return None
        label = approval_option_label(options)
        if label is None:
            return None
        answers[question_id] = {"answers": [label]}
    return answers or None


def tool_request_user_input_unavailable_answers(
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Return canned responses for non-interactive sessions so turns can continue."""
    questions = params.get("questions")
    if not isinstance(questions, list):
        return None
    answers: dict[str, Any] = {}
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("id"), str):
            return None
        answers[question["id"]] = {"answers": [NON_INTERACTIVE_TOOL_INPUT_ANSWER]}
    return answers or None


def approval_option_label(options: list[Any]) -> str | None:
    """Pick the most permissive approval option available."""
    labels = [
        option["label"]
        for option in options
        if isinstance(option, dict) and isinstance(option.get("label"), str)
    ]
    for preferred in ("Approve this Session", "Approve Once"):
        if preferred in labels:
            return preferred
    for label in labels:
        normalized = label.strip().lower()
        if normalized.startswith("approve") or normalized.startswith("allow"):
            return label
    return None


def extract_token_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull token counts from whichever nesting level Codex chose for this turn."""
    usage = absolute_token_usage_from_payload(payload)
    if usage is not None:
        return usage
    usage = turn_completed_usage_from_payload(payload)
    return usage or {}


def absolute_token_usage_from_payload(payload: Any) -> dict[str, Any] | None:
    for path in (
        ("params", "msg", "payload", "info", "total_token_usage"),
        ("params", "msg", "info", "total_token_usage"),
        ("params", "tokenUsage", "total"),
        ("tokenUsage", "total"),
    ):
        value = map_at_path(payload, path)
        if isinstance(value, dict) and integer_token_map(value):
            return value
    return None


def turn_completed_usage_from_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("method") != "turn/completed":
        return None
    direct = payload.get("usage") or map_at_path(payload, ("params", "usage"))
    return direct if isinstance(direct, dict) and integer_token_map(direct) else None


def extract_rate_limits(payload: dict[str, Any]) -> dict[str, Any] | None:
    direct = payload.get("rate_limits")
    if rate_limits_map(direct):
        return direct
    return rate_limits_from_payload(payload)


def rate_limits_from_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        if rate_limits_map(payload):
            return payload
        for value in payload.values():
            result = rate_limits_from_payload(value)
            if result is not None:
                return result
    if isinstance(payload, list):
        for value in payload:
            result = rate_limits_from_payload(value)
            if result is not None:
                return result
    return None


def rate_limits_map(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and (payload.get("limit_id") or payload.get("limit_name")) is not None
        and any(bucket in payload for bucket in ("primary", "secondary", "credits"))
    )


def map_at_path(payload: Any, path: tuple[str, ...]) -> Any:
    """Safely walk a nested payload dictionary along `path`."""
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def integer_token_map(payload: dict[str, Any]) -> bool:
    fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "promptTokens",
        "completionTokens",
    )
    return any(integer_like(payload.get(field)) is not None for field in fields)


def integer_like(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def message_summary(payload: Any) -> str | None:
    if isinstance(payload, dict):
        if isinstance(payload.get("method"), str):
            return payload["method"]
        params = payload.get("params")
        if (
            isinstance(params, dict)
            and isinstance(params.get("question"), str)
            and params["question"].strip()
        ):
            return params["question"].strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return None
