from __future__ import annotations

"""Runtime helper for workflow-triggered AI review passes."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ....coding_agents.base import AgentMessageHandler, CodingAgentRuntime
from ....coding_agents.review_models import ReviewFinding, ReviewOutput
from ....errors import ReviewError
from ....issues import Issue
from ....structured_results import StructuredTurnResult
from ....workflow.models import WorkflowSnapshot, WorkflowStateProfile
from ....workflow.profiles.review_profiles import (
    ResolvedAiReviewScope,
    WorkflowReviewType,
)
from ....workspace.ai_review.ai_review_feedback import (
    accepted_review_findings,
    ai_review_exhausted_summary,
    ai_review_feedback_prompt,
    ai_review_scope_failure_prompt,
    ai_review_scope_failure_summary,
)
from ....workspace.ai_review.ai_review_prompt import render_ai_review_prompt
from ....workspace.review.review_surface import (
    WorktreeReviewSelection,
    select_worktree_review_types,
)
from ...activity_phase import (
    AI_REVIEW_PHASE,
    emit_activity_phase_update,
)
from ...messages import AgentWorkerUpdate


@dataclass(frozen=True, slots=True)
class ExecutedAiReview:
    review_type: WorkflowReviewType
    review_output: ReviewOutput
    accepted_findings: tuple[ReviewFinding, ...]


@dataclass(frozen=True, slots=True)
class AiReviewPassResult:
    selection: WorktreeReviewSelection
    executed_reviews: tuple[ExecutedAiReview, ...]

    @property
    def accepted_findings(self) -> tuple[ReviewFinding, ...]:
        return tuple(
            finding
            for review in self.executed_reviews
            for finding in review.accepted_findings
        )

    @property
    def matched_review_types(self) -> tuple[WorkflowReviewType, ...]:
        return self.selection.matched_types


async def run_ai_review_pass(
    *,
    runtime: CodingAgentRuntime,
    workflow_snapshot: WorkflowSnapshot,
    workspace_path: str,
    issue: Issue,
    on_message: AgentMessageHandler | None = None,
) -> AiReviewPassResult:
    """Execute all currently triggered workflow review types in fresh review runs."""

    review_scope = workflow_snapshot.state_profile(issue.state)
    assert review_scope is not None
    selection = await select_worktree_review_types(
        workspace_path,
        workflow_snapshot.ai_review_types_for_state(issue.state),
        review_scope=review_scope.resolved_ai_review_scope(),
    )
    executed: list[ExecutedAiReview] = []
    for review_type in selection.matched_types:
        prompt = render_ai_review_prompt(
            issue,
            review_type,
            workflow_snapshot.definition.review_sections[review_type.prompt_ref],
            review_scope=selection.surface.review_scope,
            base_ref=selection.surface.base_ref,
            changed_paths=selection.surface.changed_paths,
            lines_changed=selection.surface.lines_changed,
        )
        review_output = await runtime.run_review(
            workspace_path,
            prompt,
            issue,
            on_message=on_message,
            model=review_type.codex.model,
            reasoning_effort=review_type.codex.reasoning_effort,
            fast_mode=review_type.codex.fast_mode,
        )
        executed.append(
            ExecutedAiReview(
                review_type=review_type,
                review_output=review_output,
                accepted_findings=accepted_review_findings(review_output),
            )
        )
    return AiReviewPassResult(
        selection=selection,
        executed_reviews=tuple(executed),
    )


async def run_ai_review_gate(
    *,
    runtime: CodingAgentRuntime,
    workflow_snapshot: WorkflowSnapshot,
    workspace_path: str,
    issue: Issue,
    profile: WorkflowStateProfile,
    queue: asyncio.Queue[Any],
    issue_id: str | None,
    feedback_attempts: int,
    failure_state: str,
    on_message: AgentMessageHandler | None,
) -> tuple[int, str, StructuredTurnResult | None] | None:
    if not profile.ai_review_refs:
        return None
    await emit_activity_phase_update(
        queue,
        issue_id,
        event="ai_review_started",
        activity_phase=AI_REVIEW_PHASE,
    )
    try:
        ai_review = await run_ai_review_pass(
            runtime=runtime,
            workflow_snapshot=workflow_snapshot,
            workspace_path=workspace_path,
            issue=issue,
            on_message=on_message,
        )
    except ReviewError as exc:
        return _ai_review_scope_failure_result(
            reason=str(exc),
            review_scope=profile.resolved_ai_review_scope(),
            feedback_attempts=feedback_attempts,
            max_feedback_loops=profile.hooks.before_complete_max_feedback_loops,
            failure_state=failure_state,
        )
    if not ai_review.matched_review_types:
        await _emit_ai_review_update(
            queue,
            issue_id,
            "ai_review_skipped",
            payload={
                "review_scope": ai_review.selection.surface.review_scope,
                "matched_review_types": [],
                "changed_paths": list(ai_review.selection.surface.changed_paths),
                "lines_changed": ai_review.selection.surface.lines_changed,
            },
        )
        return None
    await _emit_ai_review_update(
        queue,
        issue_id,
        "ai_review_completed",
        payload=_ai_review_update_payload(ai_review),
    )
    if not ai_review.accepted_findings:
        return None
    next_attempt = feedback_attempts + 1
    if next_attempt > profile.hooks.before_complete_max_feedback_loops:
        return (
            next_attempt,
            "",
            StructuredTurnResult(
                decision="blocked",
                summary=ai_review_exhausted_summary(
                    ai_review.accepted_findings,
                    profile.hooks.before_complete_max_feedback_loops,
                ),
                next_state=failure_state,
            ),
        )
    return (
        next_attempt,
        ai_review_feedback_prompt(
            findings=ai_review.accepted_findings,
            review_types=ai_review.matched_review_types,
            attempt=next_attempt,
            max_attempts=profile.hooks.before_complete_max_feedback_loops,
        ),
        None,
    )


async def _emit_ai_review_update(
    queue: asyncio.Queue[Any],
    issue_id: str | None,
    event: str,
    *,
    payload: dict[str, Any],
) -> None:
    if issue_id:
        await queue.put(
            AgentWorkerUpdate(
                issue_id,
                {
                    "event": event,
                    "timestamp": datetime.now(UTC),
                    **payload,
                },
            )
        )


def _ai_review_update_payload(ai_review: AiReviewPassResult) -> dict[str, Any]:
    return {
        "review_scope": ai_review.selection.surface.review_scope,
        "matched_review_types": [
            review_type.review_name for review_type in ai_review.matched_review_types
        ],
        "changed_paths": list(ai_review.selection.surface.changed_paths),
        "lines_changed": ai_review.selection.surface.lines_changed,
        "accepted_finding_count": len(ai_review.accepted_findings),
        "reviews": [
            {
                "review_name": review.review_type.review_name,
                "finding_count": len(review.review_output.findings),
                "accepted_finding_count": len(review.accepted_findings),
                "overall_correctness": review.review_output.overall_correctness,
                "overall_confidence_score": review.review_output.overall_confidence_score,
                "findings": [
                    {
                        "title": finding.title,
                        "body": finding.body,
                        "priority": finding.priority,
                        "confidence_score": finding.confidence_score,
                        "absolute_file_path": finding.code_location.absolute_file_path,
                        "line_start": finding.code_location.line_range.start,
                        "line_end": finding.code_location.line_range.end,
                    }
                    for finding in review.review_output.findings
                ],
            }
            for review in ai_review.executed_reviews
        ],
    }


def _ai_review_scope_failure_result(
    *,
    reason: str,
    review_scope: ResolvedAiReviewScope,
    feedback_attempts: int,
    max_feedback_loops: int,
    failure_state: str,
) -> tuple[int, str, StructuredTurnResult | None]:
    next_attempt = feedback_attempts + 1
    if next_attempt > max_feedback_loops:
        return (
            next_attempt,
            "",
            StructuredTurnResult(
                decision="blocked",
                summary=ai_review_scope_failure_summary(reason, max_feedback_loops),
                next_state=failure_state,
            ),
        )
    return (
        next_attempt,
        ai_review_scope_failure_prompt(
            reason=reason,
            review_scope=review_scope,
            attempt=next_attempt,
            max_attempts=max_feedback_loops,
        ),
        None,
    )
