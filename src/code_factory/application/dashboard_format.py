from __future__ import annotations

from math import ceil
from typing import Any

from rich.text import Text


def agents_text(running_count: int, max_agents: int) -> Text:
    return Text.assemble(
        (str(running_count), "green"),
        ("/", "bright_black"),
        (str(max_agents), "bright_black"),
    )


def tokens_text(totals: dict[str, Any]) -> Text:
    return Text.assemble(
        (f"in {format_count(int_value(totals.get('input_tokens')))}", "yellow"),
        (" | ", "bright_black"),
        (f"out {format_count(int_value(totals.get('output_tokens')))}", "yellow"),
        (" | ", "bright_black"),
        (f"total {format_count(int_value(totals.get('total_tokens')))}", "yellow"),
    )


def next_refresh_text(polling: Any) -> Text:
    if isinstance(polling, dict) and polling.get("checking?") is True:
        return Text("checking now...", style="cyan")
    if isinstance(polling, dict) and isinstance(polling.get("next_poll_in_ms"), int):
        return Text(f"{ceil(max(0, polling['next_poll_in_ms']) / 1000)}s", style="cyan")
    return Text("n/a", style="dim")


def rate_limits_text(rate_limits: Any) -> Text:
    if rate_limits is None:
        return Text("unavailable", style="dim")
    if not isinstance(rate_limits, dict):
        return Text(clean_inline(rate_limits, 80) or "unavailable", style="dim")
    return Text.assemble(
        (str(pick(rate_limits, "limit_id", "limit_name") or "unknown"), "yellow"),
        (" | ", "bright_black"),
        (f"primary {rate_limit_bucket(rate_limits.get('primary'))}", "cyan"),
        (" | ", "bright_black"),
        (f"secondary {rate_limit_bucket(rate_limits.get('secondary'))}", "cyan"),
        (" | ", "bright_black"),
        (rate_limit_credits(rate_limits.get("credits")), "green"),
    )


def rate_limit_bucket(bucket: Any) -> str:
    if not isinstance(bucket, dict):
        return "n/a"
    remaining = pick(bucket, "remaining")
    limit = pick(bucket, "limit")
    base = (
        f"{format_count(int_value(remaining))}/{format_count(int_value(limit))}"
        if int_like(remaining) and int_like(limit)
        else "n/a"
    )
    reset = pick(
        bucket,
        "reset_in_seconds",
        "resetInSeconds",
        "reset_at",
        "resetAt",
        "resets_at",
        "resetsAt",
    )
    if isinstance(reset, int | float):
        return f"{base} reset {ceil(reset)}s"
    if isinstance(reset, str) and reset.strip():
        return f"{base} reset {clean_inline(reset, 24)}"
    return base


def rate_limit_credits(credits: Any) -> str:
    if credits is None:
        return "credits n/a"
    if isinstance(credits, dict):
        if credits.get("unlimited") is True:
            return "credits unlimited"
        remaining = pick(credits, "remaining", "available", "balance")
        if remaining is None and not credits:
            return "credits none"
        return f"credits {clean_inline(remaining, 24) or 'none'}"
    return f"credits {clean_inline(credits, 24) or 'n/a'}"


def mapping_list(value: Any) -> list[dict[str, Any]]:
    return (
        [entry for entry in value if isinstance(entry, dict)]
        if isinstance(value, list)
        else []
    )


def format_runtime(seconds: Any) -> str:
    total = int_value(seconds)
    return f"{total // 60}m {total % 60}s"


def format_count(value: Any) -> str:
    return f"{int_value(value):,}"


def pick(mapping: Any, *keys: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def int_value(value: Any) -> int:
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def int_like(value: Any) -> bool:
    return isinstance(value, int | float) or (
        isinstance(value, str) and value.strip().isdigit()
    )


def clean_inline(value: Any, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_length else f"{text[: max_length - 3]}..."
