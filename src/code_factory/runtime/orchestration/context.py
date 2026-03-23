"""Protocol definition that ties dispatch and reconciliation mixins to the actor state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, ClassVar, Protocol

from ...config.models import Settings
from ...issues import Issue
from ...trackers.base import Tracker
from ...workflow.models import WorkflowSnapshot
from ...workspace import WorkspaceManager
from .models import RetryEntry, RunningEntry


class OrchestratorContext(Protocol):
    """Describes the runtime state and helpers expected by orchestration mixins."""

    FAILURE_RETRY_BASE_MS: ClassVar[int]
    _logger: logging.Logger
    queue: asyncio.Queue[Any]
    workflow_snapshot: WorkflowSnapshot
    workflow_reload_error: str | None
    tracker: Tracker
    running: dict[str, RunningEntry]
    claimed: set[str]
    retry_entries: dict[str, RetryEntry]
    completed: set[str]
    agent_totals: dict[str, int]
    agent_rate_limits: dict[str, Any] | None
    poll_check_in_progress: bool
    next_poll_due_at_ms: int | None
    poll_run_due_at_ms: int | None
    _tracker_factory: Callable[[Settings], Tracker]

    @property
    def settings(self) -> Settings: ...

    async def _reconcile_running_issues(self) -> None: ...
    async def _maybe_dispatch(self) -> None: ...
    async def _reconcile_stalled_running_issues(self) -> None: ...
    async def _reconcile_issue_state(self, issue: Issue) -> None: ...
    async def _terminate_running_issue(
        self,
        issue_id: str,
        *,
        cleanup_workspace: bool,
        reason: str,
        retry_attempt: int | None = None,
        retry_error: str | None = None,
    ) -> None: ...
    async def _handle_retry_entry(self, entry: RetryEntry) -> None: ...
    async def _refresh_retry_issue(self, issue_id: str) -> Issue | None: ...
    async def _cleanup_retry_issue_workspace(
        self, issue: Issue, workspace_path: str | None
    ) -> None: ...
    async def _handle_stopping_worker_exit(self, message, entry) -> None: ...
    async def _cleanup_workspace_after_exit(
        self, issue_id: str, workspace_path: str
    ) -> None: ...

    async def _dispatch_issue(
        self,
        issue: Issue,
        attempt: int | None = None,
        workspace_path: str | None = None,
    ) -> None: ...
    async def _dispatch_auto_issue(
        self, issue: Issue, attempt: int | None = None
    ) -> None: ...
    async def _revalidate_issue_for_dispatch(self, issue: Issue) -> Issue | None: ...
    async def _run_due_retries(self, now_ms: int) -> None: ...
    async def _run_poll_cycle(self) -> None: ...
    async def _ensure_workflow_current(self) -> None: ...

    def _release_issue_claim(self, issue_id: str) -> None: ...
    def _should_dispatch_issue(self, issue: Issue) -> bool: ...
    def _handle_worker_cleanup_complete(self, message) -> None: ...
    def _snapshot_payload(self) -> dict[str, Any]: ...
    def _integrate_agent_update(
        self, issue_id: str, update: dict[str, Any]
    ) -> None: ...
    def _record_session_completion_totals(self, entry) -> None: ...
    def _workspace_manager_for_path(self, workspace_path: str) -> WorkspaceManager: ...

    def _schedule_issue_retry(
        self,
        issue_id: str,
        attempt: int | None,
        *,
        identifier: str | None,
        error: str | None = None,
        workspace_path: str | None = None,
    ) -> None: ...

    def _monotonic_ms(self) -> int: ...

    async def _shutdown_runtime(self) -> None: ...
