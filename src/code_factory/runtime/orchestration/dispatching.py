from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from ...config import validate_dispatch_settings
from ...issues import Issue
from ...workspace import WorkspaceManager
from ..support import monotonic_ms
from ..worker import IssueWorker
from .context import OrchestratorContext
from .models import RetryEntry, RunningEntry
from .policy import (
    available_slots,
    candidate_issue,
    failure_retry_delay,
    normalize_retry_attempt,
    sort_issues_for_dispatch,
    state_slots_available,
    terminal_issue_state,
    todo_issue_blocked_by_non_terminal,
)


class DispatchingMixin:
    async def _run_poll_cycle(self: OrchestratorContext) -> None:
        try:
            await self._ensure_workflow_current()
            await self._maybe_dispatch()
        finally:
            self.poll_check_in_progress = False
            self.poll_run_due_at_ms = None
            self.next_poll_due_at_ms = (
                monotonic_ms() + self.settings.polling.interval_ms
            )

    async def _maybe_dispatch(self: OrchestratorContext) -> None:
        await self._reconcile_running_issues()
        try:
            validate_dispatch_settings(self.settings)
        except Exception as exc:
            self._logger.error("Invalid WORKFLOW.md config: %s", exc)
            return
        try:
            issues = await self.tracker.fetch_candidate_issues()
        except Exception as exc:
            self._logger.error("Failed to fetch candidate issues: %r", exc)
            return
        if available_slots(self.settings, self.running) <= 0:
            return
        for issue in sort_issues_for_dispatch(issues):
            if self._should_dispatch_issue(issue):
                await self._dispatch_issue(issue)

    def _should_dispatch_issue(self: OrchestratorContext, issue: Issue) -> bool:
        return (
            (
                candidate_issue(self.settings, issue)
                and not todo_issue_blocked_by_non_terminal(self.settings, issue)
                and issue.id not in self.claimed
                and issue.id not in self.running
                and available_slots(self.settings, self.running) > 0
                and state_slots_available(self.settings, self.running, issue)
            )
            if issue.id
            else False
        )

    async def _dispatch_issue(
        self: OrchestratorContext,
        issue: Issue,
        attempt: int | None = None,
        workspace_path: str | None = None,
    ) -> None:
        refreshed_issue = await self._revalidate_issue_for_dispatch(issue)
        if refreshed_issue is None:
            return
        profile = self.workflow_snapshot.state_profile(refreshed_issue.state)
        if profile is not None and profile.is_auto:
            await self._dispatch_auto_issue(refreshed_issue, attempt)
            return
        manager = WorkspaceManager(self.workflow_snapshot.settings)
        final_workspace_path = workspace_path or manager.workspace_path_for_issue(
            manager.safe_identifier(refreshed_issue.identifier)
        )
        worker = IssueWorker(
            issue=refreshed_issue,
            workflow_snapshot=self.workflow_snapshot,
            orchestrator_queue=self.queue,
            attempt=attempt,
            tracker=self._tracker_factory(self.workflow_snapshot.settings),
        )
        entry = RunningEntry(
            issue_id=refreshed_issue.id or "",
            identifier=refreshed_issue.identifier,
            issue=refreshed_issue,
            workspace_path=final_workspace_path,
            worker=worker,
            started_at=datetime.now(UTC),
            retry_attempt=normalize_retry_attempt(attempt),
        )
        self.running[entry.issue_id] = entry
        self.claimed.add(entry.issue_id)
        self.retry_entries.pop(entry.issue_id, None)
        asyncio.create_task(worker.run())

    async def _dispatch_auto_issue(
        self: OrchestratorContext, issue: Issue, attempt: int | None = None
    ) -> None:
        if not issue.id:
            return
        profile = self.workflow_snapshot.state_profile(issue.state)
        if profile is None or profile.auto_next_state is None:
            return
        self.claimed.add(issue.id)
        self.retry_entries.pop(issue.id, None)
        try:
            await self.tracker.update_issue_state(issue.id, profile.auto_next_state)
        except Exception as exc:
            self._schedule_issue_retry(
                issue.id,
                normalize_retry_attempt(attempt) + 1,
                identifier=issue.identifier,
                error=f"auto transition failed: {exc!r}",
            )
            return
        refreshed_issue = await self._refresh_retry_issue(issue.id)
        self.claimed.discard(issue.id)
        if refreshed_issue is None:
            self._release_issue_claim(issue.id)
            return
        if candidate_issue(
            self.settings, refreshed_issue
        ) and not todo_issue_blocked_by_non_terminal(self.settings, refreshed_issue):
            if available_slots(
                self.settings, self.running
            ) > 0 and state_slots_available(
                self.settings, self.running, refreshed_issue
            ):
                await self._dispatch_issue(refreshed_issue, attempt=attempt)
                return
            self._schedule_issue_retry(
                refreshed_issue.id or issue.id,
                normalize_retry_attempt(attempt) + 1,
                identifier=refreshed_issue.identifier,
                error="no available orchestrator slots",
            )
            return
        self._release_issue_claim(issue.id)

    async def _revalidate_issue_for_dispatch(
        self: OrchestratorContext, issue: Issue
    ) -> Issue | None:
        if not issue.id:
            return issue
        try:
            refreshed = await self.tracker.fetch_issue_states_by_ids([issue.id])
        except Exception:
            return None
        if not refreshed:
            return None
        candidate = refreshed[0]
        if candidate_issue(
            self.settings, candidate
        ) and not todo_issue_blocked_by_non_terminal(self.settings, candidate):
            return candidate
        return None

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
        try:
            issues = await self.tracker.fetch_candidate_issues()
        except Exception as exc:
            self._schedule_issue_retry(
                entry.issue_id,
                entry.attempt + 1,
                identifier=entry.identifier,
                error=f"retry poll failed: {exc!r}",
                workspace_path=entry.workspace_path,
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
        self.retry_entries[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier or (previous.identifier if previous else issue_id),
            attempt=next_attempt,
            due_at_ms=monotonic_ms() + delay_ms,
            token=uuid.uuid4().hex,
            error=error or (previous.error if previous else None),
            workspace_path=workspace_path
            or (previous.workspace_path if previous else None),
        )
        self.claimed.add(issue_id)

    def _release_issue_claim(self: OrchestratorContext, issue_id: str) -> None:
        self.claimed.discard(issue_id)
        self.retry_entries.pop(issue_id, None)
