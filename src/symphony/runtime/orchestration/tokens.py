from __future__ import annotations

from typing import Any

from .models import RunningEntry


def apply_token_delta(
    agent_totals: dict[str, int], token_delta: dict[str, int]
) -> dict[str, int]:
    return {
        "input_tokens": max(
            0, agent_totals.get("input_tokens", 0) + token_delta.get("input_tokens", 0)
        ),
        "output_tokens": max(
            0,
            agent_totals.get("output_tokens", 0) + token_delta.get("output_tokens", 0),
        ),
        "total_tokens": max(
            0, agent_totals.get("total_tokens", 0) + token_delta.get("total_tokens", 0)
        ),
        "seconds_running": max(
            0,
            agent_totals.get("seconds_running", 0)
            + token_delta.get("seconds_running", 0),
        ),
    }


def extract_token_delta(
    running_entry: RunningEntry, update: dict[str, Any]
) -> dict[str, int]:
    usage = extract_token_usage(update)
    input_usage = compute_token_delta(
        running_entry.agent_last_reported_input_tokens, get_token_usage(usage, "input")
    )
    output_usage = compute_token_delta(
        running_entry.agent_last_reported_output_tokens,
        get_token_usage(usage, "output"),
    )
    total_usage = compute_token_delta(
        running_entry.agent_last_reported_total_tokens, get_token_usage(usage, "total")
    )
    return {
        "input_tokens": input_usage[0],
        "output_tokens": output_usage[0],
        "total_tokens": total_usage[0],
        "input_reported": input_usage[1],
        "output_reported": output_usage[1],
        "total_reported": total_usage[1],
        "seconds_running": 0,
    }


def extract_token_usage(update: dict[str, Any]) -> dict[str, Any]:
    usage = update.get("token_usage")
    return usage if isinstance(usage, dict) else {}


def extract_rate_limits(update: dict[str, Any]) -> dict[str, Any] | None:
    rate_limits = update.get("rate_limits")
    return rate_limits if isinstance(rate_limits, dict) else None


def compute_token_delta(previous: int, next_total: int | None) -> tuple[int, int]:
    if isinstance(next_total, int) and next_total >= previous:
        return next_total - previous, next_total
    return 0, previous


def get_token_usage(usage: dict[str, Any], kind: str) -> int | None:
    fields_by_kind = {
        "input": [
            "input_tokens",
            "prompt_tokens",
            "input",
            "promptTokens",
            "inputTokens",
        ],
        "output": [
            "output_tokens",
            "completion_tokens",
            "output",
            "completion",
            "outputTokens",
            "completionTokens",
        ],
        "total": ["total_tokens", "total", "totalTokens"],
    }
    for field in fields_by_kind[kind]:
        value = integer_like(usage.get(field))
        if value is not None:
            return value
    return None


def integer_like(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
