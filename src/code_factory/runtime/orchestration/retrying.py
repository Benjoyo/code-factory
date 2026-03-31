"""Retry scheduling helpers extracted from dispatching to keep mixins focused."""

from __future__ import annotations

import uuid

from ...issues import Issue
from ...workspace import WorkspaceManager
from ..support import monotonic_ms
from .context import OrchestratorContext
from .failure_policy import (
    RETRY_MODE_WAIT,
    exhausted_retry_summary,
    retry_attempt_exhausted,
    transition_issue_to_failure_state,
)
from .models import RetryEntry
from .policy import (
    available_slots,
    candidate_issue,
    failure_retry_delay,
    state_slots_available,
    terminal_issue_state,
    todo_issue_blocked_by_non_terminal,
)


class RetryingMixin:
    async def _run_due_retries(self: OrchestratorContext, now_ms: int) -> None:
        due_retries = sorted(
            (
                entry
                for entry in self.retry_entries.values()
                if entry.due_at_ms <= now_ms
            ),
            key=lambda entry: entry.due_at_ms,
        )
        for entry in due_retries:
            current = self.retry_entries.get(entry.issue_id)
            if current is None or current.token != entry.token:
                continue
            self.retry_entries.pop(entry.issue_id, None)
            await self._handle_retry_entry(entry)

    async def _handle_retry_entry(self: OrchestratorContext, entry: RetryEntry) -> None:
        if retry_attempt_exhausted(
            self.workflow_snapshot, mode=entry.mode, attempt=entry.attempt
        ):
            try:
                await transition_issue_to_failure_state(
                    self.workflow_snapshot,
                    self.tracker,
                    Issue(
                        id=entry.issue_id,
                        identifier=entry.identifier,
                        state=entry.state_name,
                    ),
                    summary=exhausted_retry_summary(
                        entry.error,
                        attempt=entry.attempt,
                        max_retries=self.settings.agent.max_worker_retries,
                    ),
                    workspace_path=entry.workspace_path,
                )
            except Exception as exc:
                self._schedule_issue_retry(
                    entry.issue_id,
                    entry.attempt,
                    identifier=entry.identifier,
                    error=f"failure escalation failed: {exc!r}",
                    workspace_path=entry.workspace_path,
                    state_name=entry.state_name,
                    mode=entry.mode,
                )
            else:
                self._release_issue_claim(entry.issue_id)
            return
        try:
            issues = await self.tracker.fetch_candidate_issues()
        except Exception as exc:
            self._schedule_issue_retry(
                entry.issue_id,
                entry.attempt + 1,
                identifier=entry.identifier,
                error=f"retry poll failed: {exc!r}",
                workspace_path=entry.workspace_path,
                state_name=entry.state_name,
                mode=entry.mode,
            )
            return
        issue = next(
            (candidate for candidate in issues if candidate.id == entry.issue_id), None
        )
        if issue is None:
            issue = await self._refresh_retry_issue(entry.issue_id)
        if issue is None:
            self._release_issue_claim(entry.issue_id)
            return
        if terminal_issue_state(self.settings, issue.state):
            await self._cleanup_retry_issue_workspace(issue, entry.workspace_path)
            self._release_issue_claim(entry.issue_id)
            return
        if candidate_issue(
            self.settings, issue
        ) and not todo_issue_blocked_by_non_terminal(self.settings, issue):
            if available_slots(
                self.settings, self.running
            ) > 0 and state_slots_available(self.settings, self.running, issue):
                await self._dispatch_issue(
                    issue, attempt=entry.attempt, workspace_path=entry.workspace_path
                )
            else:
                self._schedule_issue_retry(
                    issue.id or entry.issue_id,
                    entry.attempt + 1,
                    identifier=issue.identifier,
                    error="no available orchestrator slots",
                    workspace_path=entry.workspace_path,
                    state_name=issue.state,
                    mode=RETRY_MODE_WAIT,
                )
            return
        self._release_issue_claim(entry.issue_id)

    async def _refresh_retry_issue(
        self: OrchestratorContext, issue_id: str
    ) -> Issue | None:
        try:
            issues = await self.tracker.fetch_issue_states_by_ids([issue_id])
        except Exception:
            return None
        return issues[0] if issues else None

    async def _cleanup_retry_issue_workspace(
        self: OrchestratorContext,
        issue: Issue,
        workspace_path: str | None,
    ) -> None:
        manager = (
            self._workspace_manager_for_path(workspace_path)
            if workspace_path
            else WorkspaceManager(self.workflow_snapshot.settings)
        )
        try:
            if workspace_path:
                await manager.remove(workspace_path)
            elif issue.identifier:
                await manager.remove_issue_workspaces(issue.identifier)
        except Exception:
            return

    def _schedule_issue_retry(
        self: OrchestratorContext,
        issue_id: str,
        attempt: int | None,
        *,
        identifier: str | None,
        error: str | None = None,
        workspace_path: str | None = None,
        state_name: str | None = None,
        mode: str = "failure",
    ) -> None:
        previous = self.retry_entries.get(issue_id)
        next_attempt = (
            attempt
            if isinstance(attempt, int)
            else ((previous.attempt + 1) if previous else 1)
        )
        delay_ms = failure_retry_delay(
            self.FAILURE_RETRY_BASE_MS,
            self.settings.agent.max_retry_backoff_ms,
            next_attempt,
        )
        due_at_ms = monotonic_ms() + delay_ms
        self.retry_entries[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier or (previous.identifier if previous else issue_id),
            attempt=next_attempt,
            due_at_ms=due_at_ms,
            token=uuid.uuid4().hex,
            error=error or (previous.error if previous else None),
            workspace_path=workspace_path
            or (previous.workspace_path if previous else None),
            state_name=state_name or (previous.state_name if previous else None),
            mode=mode if previous is None else mode or previous.mode,
        )
        poll_check_in_progress = getattr(self, "poll_check_in_progress", False)
        next_poll_due_at_ms = getattr(self, "next_poll_due_at_ms", None)
        poll_run_due_at_ms = getattr(self, "poll_run_due_at_ms", None)
        if (
            not poll_check_in_progress
            and isinstance(next_poll_due_at_ms, int)
            and next_poll_due_at_ms < due_at_ms
        ):
            self.next_poll_due_at_ms = due_at_ms
        if (
            poll_check_in_progress
            and isinstance(poll_run_due_at_ms, int)
            and poll_run_due_at_ms < due_at_ms
        ):
            self.poll_run_due_at_ms = due_at_ms
        self.claimed.add(issue_id)

    def _release_issue_claim(self: OrchestratorContext, issue_id: str) -> None:
        self.claimed.discard(issue_id)
        self.retry_entries.pop(issue_id, None)
