"""Helpers for retry limits and failure-state escalation."""

from __future__ import annotations

from contextlib import suppress

from ...issues import Issue
from ...structured_results import StructuredTurnResult
from ...trackers.base import Tracker
from ...workflow.models import WorkflowSnapshot
from ..worker.results import persist_state_result
from ..worker.workpad import sync_workspace_workpad

RETRY_MODE_FAILURE = "failure"
RETRY_MODE_WAIT = "wait"


def retry_attempt_exhausted(
    workflow_snapshot: WorkflowSnapshot, *, mode: str, attempt: int
) -> bool:
    return (
        mode == RETRY_MODE_FAILURE
        and attempt > workflow_snapshot.settings.agent.max_worker_retries
    )


def exhausted_retry_summary(
    reason: str | None, *, attempt: int, max_retries: int
) -> str:
    detail = reason or "unknown worker failure"
    return (
        f"Code Factory exhausted worker retries after {attempt} attempts "
        f"(limit {max_retries}). Last error: {detail}"
    )


async def transition_issue_to_failure_state(
    workflow_snapshot: WorkflowSnapshot,
    tracker: Tracker,
    issue: Issue,
    *,
    summary: str,
    workspace_path: str | None = None,
) -> None:
    target_state = workflow_snapshot.failure_state_for_state(issue.state)
    if workspace_path is not None:
        with suppress(Exception):
            await sync_workspace_workpad(
                workflow_snapshot.settings, tracker, issue, workspace_path
            )
    if issue.state:
        await persist_state_result(
            tracker,
            issue,
            issue.state,
            StructuredTurnResult(
                decision="blocked",
                summary=summary,
                next_state=target_state,
            ),
        )
    if not issue.id:
        raise RuntimeError("missing_issue_id_for_failure_transition")
    await tracker.update_issue_state(issue.id, target_state)
