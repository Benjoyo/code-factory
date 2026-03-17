from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .models import RetryEntry, RunningEntry


def snapshot_payload(
    running: dict[str, RunningEntry],
    retry_entries: dict[str, RetryEntry],
    *,
    agent_totals: dict[str, int],
    rate_limits: dict[str, Any] | None,
    poll_check_in_progress: bool,
    next_poll_due_at_ms: int | None,
    poll_interval_ms: int,
    now_ms: int,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "running": [
            running_entry_payload(issue_id, entry, now)
            for issue_id, entry in running.items()
        ],
        "retrying": [
            retry_entry_payload(issue_id, entry, now_ms)
            for issue_id, entry in retry_entries.items()
        ],
        "agent_totals": dict(agent_totals),
        "rate_limits": rate_limits,
        "polling": {
            "checking?": poll_check_in_progress,
            "next_poll_in_ms": max(0, next_poll_due_at_ms - now_ms)
            if isinstance(next_poll_due_at_ms, int)
            else None,
            "poll_interval_ms": poll_interval_ms,
        },
    }


def running_entry_payload(
    issue_id: str, entry: RunningEntry, now: datetime
) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "identifier": entry.identifier,
        "state": entry.issue.state,
        "session_id": entry.session_id,
        "runtime_pid": entry.agent_runtime_pid,
        "input_tokens": entry.agent_input_tokens,
        "output_tokens": entry.agent_output_tokens,
        "total_tokens": entry.agent_total_tokens,
        "turn_count": entry.turn_count,
        "started_at": entry.started_at,
        "last_agent_timestamp": entry.last_agent_timestamp,
        "last_agent_message": entry.last_agent_message,
        "last_agent_event": entry.last_agent_event,
        "runtime_seconds": max(0, int((now - entry.started_at).total_seconds())),
        "workspace_path": entry.workspace_path,
        "stopping": entry.stopping,
    }


def retry_entry_payload(
    issue_id: str, entry: RetryEntry, now_ms: int
) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "attempt": entry.attempt,
        "due_in_ms": max(0, entry.due_at_ms - now_ms),
        "identifier": entry.identifier,
        "error": entry.error,
        "workspace_path": entry.workspace_path,
    }
