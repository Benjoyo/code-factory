from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ...config import validate_dispatch_settings
from ...issues import Issue
from ...workspace import WorkspaceManager
from ..support import monotonic_ms
from ..worker import IssueWorker
from .context import OrchestratorContext
from .failure_policy import (
    RETRY_MODE_WAIT,
    exhausted_retry_summary,
    retry_attempt_exhausted,
    transition_issue_to_failure_state,
)
from .models import RunningEntry
from .policy import (
    available_slots,
    candidate_issue,
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
            next_attempt = normalize_retry_attempt(attempt) + 1
            if retry_attempt_exhausted(
                self.workflow_snapshot, mode="failure", attempt=next_attempt
            ):
                try:
                    await transition_issue_to_failure_state(
                        self.workflow_snapshot,
                        self.tracker,
                        issue,
                        summary=exhausted_retry_summary(
                            f"auto transition failed: {exc!r}",
                            attempt=next_attempt,
                            max_retries=self.settings.agent.max_worker_retries,
                        ),
                    )
                except Exception as escalation_exc:
                    self._schedule_issue_retry(
                        issue.id,
                        next_attempt,
                        identifier=issue.identifier,
                        error=f"failure escalation failed: {escalation_exc!r}",
                        state_name=issue.state,
                    )
                else:
                    self._release_issue_claim(issue.id)
            else:
                self._schedule_issue_retry(
                    issue.id,
                    next_attempt,
                    identifier=issue.identifier,
                    error=f"auto transition failed: {exc!r}",
                    state_name=issue.state,
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
                state_name=refreshed_issue.state,
                mode=RETRY_MODE_WAIT,
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
