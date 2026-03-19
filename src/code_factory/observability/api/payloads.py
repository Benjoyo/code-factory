"""Payload shapers for the small observability HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def state_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Convert the orchestrator snapshot into the top-level API response shape."""

    return {
        "generated_at": iso8601(datetime.now(UTC)),
        "counts": {
            "running": len(snapshot["running"]),
            "retrying": len(snapshot["retrying"]),
        },
        "workflow": snapshot.get("workflow"),
        "running": [running_entry_payload(entry) for entry in snapshot["running"]],
        "retrying": [retry_entry_payload(entry) for entry in snapshot["retrying"]],
        "agent_totals": snapshot["agent_totals"],
        "rate_limits": snapshot["rate_limits"],
    }


def issue_payload(
    issue_identifier: str, snapshot: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the detail payload for one issue if it is running or retrying."""

    running = next(
        (
            entry
            for entry in snapshot["running"]
            if entry["identifier"] == issue_identifier
        ),
        None,
    )
    retry = next(
        (
            entry
            for entry in snapshot["retrying"]
            if entry["identifier"] == issue_identifier
        ),
        None,
    )
    if running is None and retry is None:
        return None
    current = running if running is not None else retry
    assert current is not None
    return {
        "issue_identifier": issue_identifier,
        "issue_id": current["issue_id"],
        "status": "running" if running is not None else "retrying",
        "workspace": {"path": current.get("workspace_path")},
        "attempts": {
            "restart_count": max((retry or {}).get("attempt", 0) - 1, 0),
            "current_retry_attempt": (retry or {}).get("attempt", 0),
        },
        "running": running_issue_payload(running) if running else None,
        "retry": retry_issue_payload(retry) if retry else None,
        "logs": {"agent_session_logs": []},
        "recent_events": recent_events_payload(running) if running else [],
        "last_error": (retry or {}).get("error"),
        "tracked": {},
    }


def running_entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Shape a running entry into the compact list representation used by the API."""

    return {
        "issue_id": entry["issue_id"],
        "issue_identifier": entry["identifier"],
        "state": entry["state"],
        "session_id": entry["session_id"],
        "turn_count": entry["turn_count"],
        "last_event": entry["last_agent_event"],
        "last_message": humanize_agent_message(entry["last_agent_message"]),
        "started_at": iso8601(entry["started_at"]),
        "last_event_at": iso8601(entry["last_agent_timestamp"]),
        "tokens": {
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            "total_tokens": entry["total_tokens"],
        },
        "runtime_pid": entry.get("runtime_pid"),
        "workspace_path": entry.get("workspace_path"),
        "stopping": entry.get("stopping", False),
    }


def retry_entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Shape a retry entry into the list representation used by the API."""

    return {
        "issue_id": entry["issue_id"],
        "issue_identifier": entry["identifier"],
        "attempt": entry["attempt"],
        "due_at": due_at_iso8601(entry["due_in_ms"]),
        "error": entry["error"],
        "workspace_path": entry.get("workspace_path"),
    }


def running_issue_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the nested running section for the per-issue endpoint."""

    return {
        "session_id": entry["session_id"],
        "turn_count": entry["turn_count"],
        "state": entry["state"],
        "started_at": iso8601(entry["started_at"]),
        "last_event": entry["last_agent_event"],
        "last_message": humanize_agent_message(entry["last_agent_message"]),
        "last_event_at": iso8601(entry["last_agent_timestamp"]),
        "tokens": {
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            "total_tokens": entry["total_tokens"],
        },
    }


def retry_issue_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the nested retry section for the per-issue endpoint."""

    return {
        "attempt": entry["attempt"],
        "due_at": due_at_iso8601(entry["due_in_ms"]),
        "error": entry["error"],
    }


def recent_events_payload(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose the latest event as a single-item timeline for now."""

    at = iso8601(entry["last_agent_timestamp"])
    if at is None:
        return []
    return [
        {
            "at": at,
            "event": entry["last_agent_event"],
            "message": humanize_agent_message(entry["last_agent_message"]),
        }
    ]


def humanize_agent_message(message: Any) -> Any:
    """Flatten nested agent message payloads into the user-facing text value."""

    if isinstance(message, dict):
        nested = message.get("message")
        return nested if not isinstance(nested, str) else nested
    return message


def iso8601(value: Any) -> str | None:
    """Serialize datetimes in a stable UTC `Z` form for API consumers."""

    if isinstance(value, datetime):
        return (
            value.astimezone(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return None


def due_at_iso8601(due_in_ms: Any) -> str | None:
    """Translate a relative retry delay into an absolute due timestamp."""

    if isinstance(due_in_ms, int):
        return iso8601(datetime.now(UTC) + timedelta(milliseconds=due_in_ms))
    return None
