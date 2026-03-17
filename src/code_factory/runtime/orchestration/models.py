"""State holders for retries and running issues managed by the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ...issues import Issue


@dataclass(slots=True)
class RetryEntry:
    """Records when the orchestrator should attempt to recreate work for an issue."""

    issue_id: str
    identifier: str | None
    attempt: int
    due_at_ms: int
    token: str
    error: str | None = None
    workspace_path: str | None = None


@dataclass(slots=True)
class RunningEntry:
    """Tracks metadata for a currently running issue worker session."""

    issue_id: str
    identifier: str | None
    issue: Issue
    workspace_path: str
    worker: Any
    started_at: datetime
    retry_attempt: int = 0
    session_id: str | None = None
    last_agent_message: Any = None
    last_agent_timestamp: datetime | None = None
    last_agent_event: str | None = None
    agent_runtime_pid: str | None = None
    agent_input_tokens: int = 0
    agent_output_tokens: int = 0
    agent_total_tokens: int = 0
    agent_last_reported_input_tokens: int = 0
    agent_last_reported_output_tokens: int = 0
    agent_last_reported_total_tokens: int = 0
    turn_count: int = 0
    stopping: bool = False
    stop_requested_at: datetime | None = None
    cleanup_workspace: bool = False
    last_stop_reason: str | None = None
    post_exit_retry_attempt: int | None = None
    post_exit_retry_error: str | None = None
