"""Prompt rendering for workflow-configured AI review turns."""

from __future__ import annotations

from collections.abc import Sequence

from ...issues import Issue
from ...prompts.review_assets import base_review_prompt
from ...workflow.profiles.review_profiles import (
    AI_REVIEW_SCOPE_BRANCH,
    AI_REVIEW_SCOPE_WORKTREE,
    ResolvedAiReviewScope,
    WorkflowReviewType,
)

DETAIL_GUIDELINES_ANCHOR = "Below are some more detailed guidelines that you should apply to this specific review."


def render_ai_review_prompt(
    issue: Issue,
    review_type: WorkflowReviewType,
    overlay_prompt: str,
    *,
    review_scope: ResolvedAiReviewScope,
    base_ref: str | None,
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
            review_scope=review_scope,
            base_ref=base_ref,
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
    review_scope: ResolvedAiReviewScope,
    base_ref: str | None,
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
        f"- Review scope: {review_scope}",
        f"- Lines changed: {lines_changed}",
        _review_scope_instruction(review_scope, base_ref),
        "",
        "Changed paths:",
        changed_paths_text,
        "",
        "### Ticket metadata",
        f"- Identifier: {issue.identifier or '<unknown>'}",
        f"- Title: {issue.title or '<untitled>'}",
        "",
        "### Ticket description",
        description,
        "",
        "### Workflow-specific review focus",
        review_focus,
    ]
    return "\n".join(lines)


def _review_scope_instruction(
    review_scope: ResolvedAiReviewScope, base_ref: str | None
) -> str:
    if review_scope == AI_REVIEW_SCOPE_WORKTREE:
        return "- Review only the current workspace diff."
    assert review_scope == AI_REVIEW_SCOPE_BRANCH
    resolved_base_ref = base_ref or "<unknown>"
    return (
        "- Review the committed branch diff from the merge-base with "
        f"`{resolved_base_ref}` to `HEAD`."
    )
