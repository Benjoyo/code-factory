"""Mixin that keeps orchestrator running issues aligned with tracker state."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ...issues import Issue
from ...workspace import WorkspaceManager
from ..messages import WorkerExited
from ..support import maybe_aclose
from .context import OrchestratorContext
from .policy import active_issue_state, next_retry_attempt, terminal_issue_state
from .snapshot import snapshot_payload
from .tokens import apply_token_delta, extract_rate_limits, extract_token_delta


class ReconciliationMixin:
    """Tracks running issues, terminates stalled work, and aggregates session metrics."""

    async def _reconcile_running_issues(self: OrchestratorContext) -> None:
        """Refresh running issues from the tracker and terminate those that dropped."""
        await self._reconcile_stalled_running_issues()
        running_ids = list(self.running.keys())
        if not running_ids:
            return
        try:
            issues = await self.tracker.fetch_issue_states_by_ids(running_ids)
        except Exception as exc:
            self._logger.debug(
                "Failed to refresh running issue states: %r; keeping active workers",
                exc,
            )
            return
        visible_ids = {issue.id for issue in issues if issue.id}
        for issue in issues:
            await self._reconcile_issue_state(issue)
        for issue_id in running_ids:
            if issue_id not in visible_ids:
                await self._terminate_running_issue(
                    issue_id, cleanup_workspace=False, reason="missing"
                )

    async def _reconcile_issue_state(self: OrchestratorContext, issue: Issue) -> None:
        """Ensure the tracked entry matches the latest issue state."""
        if terminal_issue_state(self.settings, issue.state):
            await self._terminate_running_issue(
                issue.id or "", cleanup_workspace=True, reason="terminal"
            )
        elif not issue.assigned_to_worker:
            await self._terminate_running_issue(
                issue.id or "", cleanup_workspace=False, reason="reassigned"
            )
        elif active_issue_state(self.settings, issue.state):
            entry = self.running.get(issue.id or "")
            if entry is not None:
                entry.issue = issue
        else:
            await self._terminate_running_issue(
                issue.id or "", cleanup_workspace=False, reason="non_active"
            )

    async def _reconcile_stalled_running_issues(self: OrchestratorContext) -> None:
        """Terminate agents that have not reported activity within the stall timeout."""
        timeout_ms = self.settings.coding_agent.stall_timeout_ms
        if timeout_ms <= 0:
            return
        now = datetime.now(UTC)
        for issue_id, entry in list(self.running.items()):
            if entry.stopping:
                continue
            last_activity = entry.last_agent_timestamp or entry.started_at
            # Guard against clock skew producing negative durations when the agent just reported.
            elapsed_ms = max(0, int((now - last_activity).total_seconds() * 1000))
            if elapsed_ms > timeout_ms:
                await self._terminate_running_issue(
                    issue_id,
                    cleanup_workspace=False,
                    reason="stall",
                    retry_attempt=next_retry_attempt(entry),
                    retry_error=f"stalled for {elapsed_ms}ms without agent activity",
                )

    async def _terminate_running_issue(
        self: OrchestratorContext,
        issue_id: str,
        *,
        cleanup_workspace: bool,
        reason: str,
        retry_attempt: int | None = None,
        retry_error: str | None = None,
    ) -> None:
        """Mark an entry for stopping, optionally keeping around cleanup/retry metadata."""
        entry = self.running.get(issue_id)
        if entry is None:
            self._release_issue_claim(issue_id)
            return
        entry.cleanup_workspace = entry.cleanup_workspace or cleanup_workspace
        entry.last_stop_reason = reason
        entry.stop_requested_at = datetime.now(UTC)
        entry.stopping = True
        entry.post_exit_retry_attempt = (
            retry_attempt
            if retry_attempt is not None
            else entry.post_exit_retry_attempt
        )
        entry.post_exit_retry_error = (
            retry_error if retry_attempt is not None else entry.post_exit_retry_error
        )
        asyncio.create_task(entry.worker.stop(reason))

    async def _handle_worker_exited(
        self: OrchestratorContext, message: WorkerExited
    ) -> None:
        """Process an exited worker, recording totals and scheduling retries."""
        entry = self.running.get(message.issue_id)
        if entry is None:
            return
        self._record_session_completion_totals(entry)
        if entry.stopping:
            await self._handle_stopping_worker_exit(message, entry)
            return
        self.running.pop(message.issue_id, None)
        if message.completed:
            self.completed.add(message.issue_id)
            self._release_issue_claim(message.issue_id)
        elif message.normal:
            await self._retry_or_escalate_worker_exit(
                entry,
                attempt=next_retry_attempt(entry),
                error="worker exited without completing a state transition",
            )
        else:
            await self._retry_or_escalate_worker_exit(
                entry,
                attempt=next_retry_attempt(entry),
                error=f"agent exited: {message.reason or 'unknown'}",
            )

    def _snapshot_payload(self: OrchestratorContext) -> dict[str, Any]:
        """Build the orchestrator snapshot shown to dashboards and APIs."""
        return snapshot_payload(
            self.running,
            self.retry_entries,
            workflow_snapshot=self.workflow_snapshot,
            workflow_reload_error=self.workflow_reload_error,
            agent_totals=self.agent_totals,
            rate_limits=self.agent_rate_limits,
            poll_check_in_progress=self.poll_check_in_progress,
            next_poll_due_at_ms=self.next_poll_due_at_ms,
            poll_interval_ms=self.settings.polling.interval_ms,
            now_ms=self._monotonic_ms(),
        )

    def _integrate_agent_update(
        self: OrchestratorContext, issue_id: str, update: dict[str, Any]
    ) -> None:
        """Merge incoming agent telemetry into the running entry and global counters."""
        entry = self.running.get(issue_id)
        if entry is None:
            return
        token_delta = extract_token_delta(entry, update)
        entry.last_agent_timestamp = update["timestamp"]
        entry.last_agent_message = {
            "event": update.get("event"),
            "message": update.get("message_summary")
            or update.get("payload")
            or update.get("raw"),
            "timestamp": update.get("timestamp"),
        }
        if isinstance(update.get("session_id"), str):
            # Count only transitions to a new session to keep turn totals accurate.
            entry.turn_count = (
                entry.turn_count
                if update["session_id"] == entry.session_id
                else entry.turn_count + 1
            )
            entry.session_id = update["session_id"]
        if isinstance(update.get("thread_id"), str):
            entry.thread_id = update["thread_id"]
        if isinstance(update.get("turn_id"), str):
            entry.turn_id = update["turn_id"]
        entry.last_agent_event = update.get("event")
        if update.get("event") in {"turn_completed", "turn_failed", "turn_cancelled"}:
            entry.turn_id = None
        if update.get("runtime_pid") is not None:
            entry.agent_runtime_pid = str(update["runtime_pid"])
        entry.agent_input_tokens += token_delta["input_tokens"]
        entry.agent_output_tokens += token_delta["output_tokens"]
        entry.agent_total_tokens += token_delta["total_tokens"]
        entry.agent_last_reported_input_tokens = max(
            entry.agent_last_reported_input_tokens, token_delta["input_reported"]
        )
        entry.agent_last_reported_output_tokens = max(
            entry.agent_last_reported_output_tokens, token_delta["output_reported"]
        )
        entry.agent_last_reported_total_tokens = max(
            entry.agent_last_reported_total_tokens, token_delta["total_reported"]
        )
        if update.get("event") == "session_started" and not isinstance(
            update.get("session_id"), str
        ):
            entry.turn_count += 1
        self.agent_totals = apply_token_delta(self.agent_totals, token_delta)
        # Replace cached rate-limit window when the agent reports new values.
        rate_limits = extract_rate_limits(update)
        if rate_limits is not None:
            self.agent_rate_limits = rate_limits

    def _record_session_completion_totals(self: OrchestratorContext, entry) -> None:
        """Add runtime seconds to the global total when an entry ends."""
        runtime_seconds = max(
            0, int((datetime.now(UTC) - entry.started_at).total_seconds())
        )
        self.agent_totals = apply_token_delta(
            self.agent_totals,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "seconds_running": runtime_seconds,
            },
        )

    def _workspace_manager_for_path(
        self: OrchestratorContext, workspace_path: str
    ) -> WorkspaceManager:
        """Build a WorkspaceManager rooted at the directory containing a path."""
        workspace_root = os.path.dirname(workspace_path)
        settings = replace(
            self.workflow_snapshot.settings,
            workspace=replace(
                self.workflow_snapshot.settings.workspace, root=workspace_root
            ),
        )
        return WorkspaceManager(settings)

    async def _shutdown_runtime(self: OrchestratorContext) -> None:
        """Stop all running workers and close the tracker when shutting down."""
        for entry in list(self.running.values()):
            with contextlib.suppress(Exception):
                await entry.worker.stop("shutdown")
        await maybe_aclose(self.tracker)
