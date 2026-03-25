"""Worker-exit recovery helpers extracted from reconciliation."""

from __future__ import annotations

import asyncio

from ..messages import WorkerCleanupComplete
from .context import OrchestratorContext
from .failure_policy import (
    exhausted_retry_summary,
    retry_attempt_exhausted,
    transition_issue_to_failure_state,
)


class RecoveryMixin:
    async def _handle_stopping_worker_exit(
        self: OrchestratorContext, message, entry
    ) -> None:
        if entry.cleanup_workspace and entry.workspace_path:
            asyncio.create_task(
                self._cleanup_workspace_after_exit(
                    message.issue_id, entry.workspace_path
                )
            )
            return
        self.running.pop(message.issue_id, None)
        if entry.post_exit_retry_attempt is not None:
            await self._retry_or_escalate_worker_exit(
                entry,
                attempt=entry.post_exit_retry_attempt,
                error=entry.post_exit_retry_error,
            )
        else:
            self._release_issue_claim(message.issue_id)

    async def _cleanup_workspace_after_exit(
        self: OrchestratorContext, issue_id: str, workspace_path: str
    ) -> None:
        manager = self._workspace_manager_for_path(workspace_path)
        error: str | None = None
        try:
            await manager.remove(workspace_path)
        except Exception as exc:
            error = repr(exc)
        await self.queue.put(WorkerCleanupComplete(issue_id, workspace_path, error))

    def _handle_worker_cleanup_complete(
        self: OrchestratorContext, message: WorkerCleanupComplete
    ) -> None:
        self.running.pop(message.issue_id, None)
        self.retry_entries.pop(message.issue_id, None)
        self._release_issue_claim(message.issue_id)

    async def _retry_or_escalate_worker_exit(
        self: OrchestratorContext, entry, *, attempt: int, error: str | None
    ) -> None:
        if retry_attempt_exhausted(
            self.workflow_snapshot, mode="failure", attempt=attempt
        ):
            try:
                await transition_issue_to_failure_state(
                    self.workflow_snapshot,
                    self.tracker,
                    entry.issue,
                    summary=exhausted_retry_summary(
                        error,
                        attempt=attempt,
                        max_retries=self.settings.agent.max_worker_retries,
                    ),
                    workspace_path=entry.workspace_path,
                )
            except Exception as exc:
                self._schedule_issue_retry(
                    entry.issue_id,
                    attempt,
                    identifier=entry.identifier,
                    error=f"failure escalation failed: {exc!r}",
                    workspace_path=entry.workspace_path,
                    state_name=entry.issue.state,
                )
            else:
                self.completed.add(entry.issue_id)
                self._release_issue_claim(entry.issue_id)
            return
        self._schedule_issue_retry(
            entry.issue_id,
            attempt,
            identifier=entry.identifier,
            error=error,
            workspace_path=entry.workspace_path,
            state_name=entry.issue.state,
        )
