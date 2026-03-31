"""Filtering and repair-feedback synthesis for AI review findings."""

from __future__ import annotations

from collections.abc import Sequence

from ..coding_agents.review_models import ReviewFinding, ReviewOutput
from ..workflow.review_profiles import WorkflowReviewType

AI_REVIEW_CONFIDENCE_THRESHOLD = 0.90
AI_REVIEW_PRIORITY_THRESHOLD = 1  # P1 is the highest priority


def accepted_review_findings(review_output: ReviewOutput) -> tuple[ReviewFinding, ...]:
    """Keep only findings that clear the internal confidence floor."""

    return tuple(
        finding
        for finding in review_output.findings
        if finding.confidence_score >= AI_REVIEW_CONFIDENCE_THRESHOLD
        and finding.priority is not None
        and finding.priority <= AI_REVIEW_PRIORITY_THRESHOLD
    )


def ai_review_feedback_prompt(
    *,
    findings: Sequence[ReviewFinding],
    review_types: Sequence[WorkflowReviewType],
    attempt: int,
    max_attempts: int,
) -> str:
    """Render the repair prompt fed back into the implementing agent."""

    review_names = ", ".join(review_type.review_name for review_type in review_types)
    rendered_findings = "\n\n".join(_render_finding(finding) for finding in findings)
    return (
        "An AI review blocked completion for this workflow state.\n"
        f"Triggered review types: {review_names or '<unknown>'}.\n"
        f"Feedback attempt {attempt} of {max_attempts}.\n"
        "Address every valid finding below thoughtfully, re-run the necessary validation, and then emit the required structured result again.\n"
        "Resolve review findings with durable, maintainable fixes. Do not apply quick patches, workaround logic, or narrow symptom-only edits. "
        "No bandaids, no comment-satisfying fake fixes, no special-case branching unless justified. "
        "Fix the underlying issue while preserving or improving design quality, keep the solution clean and minimal, and update tests as needed.\n\n"
        "Accepted review findings:\n"
        f"{rendered_findings}"
    )


def ai_review_exhausted_summary(
    findings: Sequence[ReviewFinding],
    max_feedback_loops: int,
) -> str:
    detail = findings[0].title if findings else "review still reported blocking issues"
    return (
        "Code Factory exhausted AI review repair loops after "
        f"{max_feedback_loops} attempt(s). Last accepted finding: {detail}"
    )


def _render_finding(finding: ReviewFinding) -> str:
    line_range = finding.code_location.line_range
    location = (
        f"{finding.code_location.absolute_file_path}:"
        f"{line_range.start}-{line_range.end}"
    )
    priority = f"P{finding.priority}" if finding.priority is not None else "P?"
    return (
        f"- [{priority}] {finding.title}\n"
        f"  Location: {location}\n"
        f"  Details: {finding.body}"
    )
