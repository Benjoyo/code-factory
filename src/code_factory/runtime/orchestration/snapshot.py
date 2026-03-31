"""Helpers that build the orchestrator snapshot payload consumed by dashboards."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ...workflow.models import WorkflowSnapshot
from .models import RetryEntry, RunningEntry


def snapshot_payload(
    running: dict[str, RunningEntry],
    retry_entries: dict[str, RetryEntry],
    *,
    workflow_snapshot: WorkflowSnapshot | None = None,
    workflow_reload_error: str | None = None,
    agent_totals: dict[str, int],
    rate_limits: dict[str, Any] | None,
    poll_check_in_progress: bool,
    next_poll_due_at_ms: int | None,
    poll_interval_ms: int,
    now_ms: int,
) -> dict[str, Any]:
    """Compose the full payload that surfaces orchestrator state for clients."""
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
        "workflow": workflow_payload(workflow_snapshot, workflow_reload_error),
        "polling": {
            "checking?": poll_check_in_progress,
            "next_poll_in_ms": max(0, next_poll_due_at_ms - now_ms)
            if isinstance(next_poll_due_at_ms, int)
            else None,
            "poll_interval_ms": poll_interval_ms,
        },
    }


def workflow_payload(
    snapshot: WorkflowSnapshot | None, reload_error: str | None
) -> dict[str, Any]:
    if snapshot is None:
        return {"reload_error": reload_error}
    settings = snapshot.settings
    return {
        "version": snapshot.version,
        "path": snapshot.path,
        "loaded_at": iso8601(snapshot.loaded_at),
        "reload_error": reload_error,
        "agent": {
            "max_concurrent_agents": settings.agent.max_concurrent_agents,
            "max_concurrent_agents_by_state": dict(
                settings.agent.max_concurrent_agents_by_state
            ),
        },
        "tracker": {
            "kind": settings.tracker.kind,
            "project_slug": settings.tracker.project_slug,
            "active_states": list(settings.tracker.active_states),
        },
        "terminal_states": list(settings.terminal_states),
        "workspace": {"root": settings.workspace.root},
        "observability": {
            "dashboard_enabled": settings.observability.dashboard_enabled,
            "refresh_ms": settings.observability.refresh_ms,
        },
        "server": {
            "host": settings.server.host,
            "port": settings.server.port,
        },
    }


def iso8601(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def running_entry_payload(
    issue_id: str, entry: RunningEntry, now: datetime
) -> dict[str, Any]:
    """Render a single running issue row for the orchestrator summary."""
    return {
        "issue_id": issue_id,
        "identifier": entry.identifier,
        "state": entry.issue.state,
        "session_id": entry.session_id,
        "thread_id": entry.thread_id,
        "turn_id": entry.turn_id,
        "runtime_pid": entry.agent_runtime_pid,
        "input_tokens": entry.agent_input_tokens,
        "output_tokens": entry.agent_output_tokens,
        "total_tokens": entry.agent_total_tokens,
        "turn_count": entry.turn_count,
        "activity_phase": entry.activity_phase,
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
    """Serialize pending retry metadata for the orchestrator view."""
    return {
        "issue_id": issue_id,
        "attempt": entry.attempt,
        "due_in_ms": max(0, entry.due_at_ms - now_ms),
        "identifier": entry.identifier,
        "error": entry.error,
        "workspace_path": entry.workspace_path,
    }
