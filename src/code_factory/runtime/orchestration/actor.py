"""Orchestrator actor that polls trackers, manages workers, and surfaces runtime snapshots."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from ...config.models import Settings
from ...trackers.base import Tracker, build_tracker
from ...workflow.models import WorkflowSnapshot
from ...workspace import WorkspaceManager
from ..messages import (
    AgentWorkerUpdate,
    RefreshRequest,
    Shutdown,
    SnapshotRequest,
    WorkerCleanupComplete,
    WorkerExited,
    WorkflowReloadError,
    WorkflowUpdated,
)
from ..support import maybe_aclose, monotonic_ms
from .dispatching import DispatchingMixin
from .models import RetryEntry, RunningEntry
from .reconciliation import ReconciliationMixin

LOGGER = logging.getLogger(__name__)


class OrchestratorActor(DispatchingMixin, ReconciliationMixin):
    """Actor responsible for synchronizing tracker data, dispatching workers, and replying to clients."""

    FAILURE_RETRY_BASE_MS: ClassVar[int] = 10_000
    POLL_TRANSITION_RENDER_DELAY_MS: ClassVar[int] = 20
    EMPTY_AGENT_TOTALS: ClassVar[dict[str, int]] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "seconds_running": 0,
    }

    def __init__(
        self,
        workflow_snapshot: WorkflowSnapshot,
        *,
        tracker_factory: Callable[[Settings], Tracker] | None = None,
        reload_workflow_if_changed: Callable[[], Awaitable[WorkflowSnapshot | None]]
        | None = None,
    ) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self._logger = LOGGER
        self.workflow_snapshot = workflow_snapshot
        self._tracker_factory = tracker_factory or (
            lambda settings: build_tracker(settings)
        )
        self._reload_workflow_if_changed = reload_workflow_if_changed
        self.tracker: Tracker = self._tracker_factory(workflow_snapshot.settings)
        self.running: dict[str, RunningEntry] = {}
        self.claimed: set[str] = set()
        self.retry_entries: dict[str, RetryEntry] = {}
        self.completed: set[str] = set()
        self.agent_totals = dict(self.EMPTY_AGENT_TOTALS)
        self.agent_rate_limits: dict[str, Any] | None = None
        self.workflow_reload_error: str | None = None
        now_ms = monotonic_ms()
        self.next_poll_due_at_ms: int | None = now_ms
        self.poll_run_due_at_ms: int | None = None
        self.poll_check_in_progress = False
        self._shutdown = False

    @property
    def settings(self) -> Settings:
        """Convenience access to the current workflow settings."""
        return self.workflow_snapshot.settings

    async def snapshot(self) -> dict[str, Any]:
        """Build an updated snapshot by queueing a SnapshotRequest."""
        return await self._request_reply(SnapshotRequest)

    def snapshot_now(self) -> dict[str, Any]:
        """Immediately return the most recent orchestrator snapshot without queuing."""
        return self._snapshot_payload()

    async def request_refresh(self) -> dict[str, Any]:
        """Ask the orchestrator to coalesce a new poll/reconcile cycle."""
        return await self._request_reply(RefreshRequest)

    async def notify_workflow_updated(self, snapshot: WorkflowSnapshot) -> None:
        """Publish a workflow update event so the actor can swap snapshots."""
        await self.queue.put(WorkflowUpdated(snapshot))

    async def notify_workflow_reload_error(self, error: Any) -> None:
        """Record reload failures so callers can see the message via SnapshotRequest."""
        await self.queue.put(WorkflowReloadError(error))

    async def startup_terminal_workspace_cleanup(self) -> None:
        """Clean up any leftover workspaces for issues in terminal states at startup."""
        try:
            issues = await self.tracker.fetch_issues_by_states(
                list(self.settings.tracker.terminal_states)
            )
        except Exception as exc:
            LOGGER.warning(
                "Skipping startup terminal workspace cleanup; failed to fetch terminal issues: %r",
                exc,
            )
            return
        manager = WorkspaceManager(self.settings)
        for issue in issues:
            if issue.identifier:
                with contextlib.suppress(Exception):
                    await manager.remove_issue_workspaces(issue.identifier)

    async def shutdown(self) -> None:
        """Request the orchestrator loop to stop processing and drain the queue."""
        await self._request_reply(Shutdown)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Drive the orchestrator event loop until a shutdown or external stop request."""
        try:
            while not self._shutdown and not stop_event.is_set():
                await self._run_once()
        finally:
            await self._shutdown_runtime()

    async def _run_once(self) -> None:
        """Wait for the next queue message (or time out) and handle it."""
        timeout = self._next_timeout_seconds()
        try:
            message = await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            await self._handle_due_deadlines()
            return
        await self._handle_message(message)

    async def _request_reply(self, message_type):
        """Enqueue a message that expects the orchestrator to fulfill the future."""
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self.queue.put(message_type(future))
        return await future

    async def _handle_message(self, message: Any) -> None:
        """Route the incoming queue message to the appropriate handler."""
        if isinstance(message, WorkflowUpdated):
            await self._replace_workflow(message.snapshot)
        elif isinstance(message, WorkflowReloadError):
            self.workflow_reload_error = repr(message.error)
        elif isinstance(message, AgentWorkerUpdate):
            self._integrate_agent_update(message.issue_id, message.update)
        elif isinstance(message, WorkerExited):
            await self._handle_worker_exited(message)
        elif isinstance(message, WorkerCleanupComplete):
            self._handle_worker_cleanup_complete(message)
        elif isinstance(message, SnapshotRequest):
            message.future.set_result(self._snapshot_payload())
        elif isinstance(message, RefreshRequest):
            await self._ensure_workflow_current()
            message.future.set_result(self._queue_refresh())
        elif isinstance(
            message, Shutdown
        ):  # pragma: no branch - terminal branch in dispatch chain
            self._shutdown = True
            message.future.set_result(True)

    async def _replace_workflow(self, snapshot: WorkflowSnapshot) -> None:
        if snapshot.version < self.workflow_snapshot.version or (
            snapshot.version == self.workflow_snapshot.version
            and snapshot.path == self.workflow_snapshot.path
            and snapshot.stamp == self.workflow_snapshot.stamp
        ):
            return
        self.workflow_snapshot = snapshot
        self.workflow_reload_error = None
        old_tracker = self.tracker
        self.tracker = self._tracker_factory(snapshot.settings)
        await maybe_aclose(old_tracker)

    def _queue_refresh(self) -> dict[str, Any]:
        """Produce a refresh response indicating whether the run was coalesced."""
        now_ms = monotonic_ms()
        already_due = (
            isinstance(self.next_poll_due_at_ms, int)
            and self.next_poll_due_at_ms <= now_ms
        )
        # Coalesce requests already in progress so we do not duplicate work.
        coalesced = self.poll_check_in_progress or already_due
        if not coalesced:
            self.next_poll_due_at_ms = now_ms
        return {
            "queued": True,
            "coalesced": coalesced,
            "requested_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "operations": ["poll", "reconcile"],
        }

    async def _handle_due_deadlines(self) -> None:
        """Run polls or retries when their deadlines have become due."""
        now_ms = monotonic_ms()
        if (
            self.poll_check_in_progress
            and isinstance(self.poll_run_due_at_ms, int)
            and self.poll_run_due_at_ms <= now_ms
        ):
            await self._run_poll_cycle()
            return
        if (
            not self.poll_check_in_progress
            and isinstance(self.next_poll_due_at_ms, int)
            and self.next_poll_due_at_ms <= now_ms
        ):
            self.poll_check_in_progress = True
            self.next_poll_due_at_ms = None
            self.poll_run_due_at_ms = now_ms + self.POLL_TRANSITION_RENDER_DELAY_MS
            return
        await self._run_due_retries(now_ms)

    def _next_timeout_seconds(self) -> float | None:
        """Choose how long to wait for the next queue message based on upcoming deadlines."""
        deadlines = [
            deadline
            for deadline in (self.next_poll_due_at_ms, self.poll_run_due_at_ms)
            if isinstance(deadline, int)
        ]
        deadlines.extend(entry.due_at_ms for entry in self.retry_entries.values())
        if not deadlines:
            return None
        return max(0, min(deadlines) - monotonic_ms()) / 1000

    def _monotonic_ms(self) -> int:
        return monotonic_ms()

    async def _ensure_workflow_current(self) -> None:
        refresher = self._reload_workflow_if_changed
        if refresher is None:
            return
        snapshot = await refresher()
        if snapshot is not None:
            await self._replace_workflow(snapshot)
