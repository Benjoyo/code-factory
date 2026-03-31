"""Prompt rendering for workflow-configured AI review turns."""

from __future__ import annotations

from collections.abc import Sequence

from ..issues import Issue
from ..prompts.review_assets import base_review_prompt
from ..workflow.review_profiles import WorkflowReviewType

DETAIL_GUIDELINES_ANCHOR = "Below are some more detailed guidelines that you should apply to this specific review."


def render_ai_review_prompt(
    issue: Issue,
    review_type: WorkflowReviewType,
    overlay_prompt: str,
    *,
    changed_paths: Sequence[str],
    lines_changed: int,
) -> str:
    """Render the ticket-aware request passed into the Codex review turn."""

    base_prompt = base_review_prompt()
    prefix, separator, suffix = base_prompt.partition(DETAIL_GUIDELINES_ANCHOR)
    if not separator:
        raise RuntimeError("vendored_review_prompt_missing_detail_guidelines_anchor")
    return (
        prefix.rstrip()
        + "\n\n"
        + _render_code_factory_review_section(
            issue=issue,
            review_type=review_type,
            overlay_prompt=overlay_prompt,
            changed_paths=changed_paths,
            lines_changed=lines_changed,
        )
        + "\n\n"
        + separator
        + suffix
        + "\n\nReturn only schema-valid JSON that matches the configured output schema.\n"
    )


def _render_code_factory_review_section(
    *,
    issue: Issue,
    review_type: WorkflowReviewType,
    overlay_prompt: str,
    changed_paths: Sequence[str],
    lines_changed: int,
) -> str:
    description = (
        issue.description.strip() if isinstance(issue.description, str) else ""
    ) or "<none>"
    branch_name = issue.branch_name or "<unknown>"
    changed_paths_text = "\n".join(f"- {path}" for path in changed_paths) or "- <none>"
    labels = ", ".join(issue.labels) if issue.labels else "<none>"
    review_focus = overlay_prompt.strip() or "<none>"
    lines = [
        "## Code Factory review context",
        "",
        "### Review scope",
        f"- Review type: {review_type.review_name}",
        f"- Lines changed: {lines_changed}",
        "- Review only the current uncommitted diff in the workspace.",
        "",
        "Changed paths:",
        changed_paths_text,
        "",
        "### Ticket metadata",
        f"- Identifier: {issue.identifier or '<unknown>'}",
        f"- Title: {issue.title or '<untitled>'}",
        f"- State: {issue.state or '<unknown>'}",
        f"- Branch: {branch_name}",
        f"- Labels: {labels}",
        "",
        "### Ticket description",
        description,
        "",
        "### Workflow-specific review focus",
        review_focus,
    ]
    return "\n".join(lines)
