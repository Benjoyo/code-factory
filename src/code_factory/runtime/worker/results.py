"""Helpers for persisted state results and enriched prompt context."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from ...issues import Issue, IssueComment
from ...structured_results import (
    RESULT_COMMENT_PREFIX,
    StructuredTurnResult,
    parse_result_comment,
    render_result_comment,
)
from ...trackers.base import Tracker

LOGGER = logging.getLogger(__name__)


async def persist_state_result(
    tracker: Tracker,
    issue: Issue,
    state_name: str,
    result: StructuredTurnResult,
) -> None:
    """Create or update the persisted result comment for one issue/state pair."""

    if not issue.id:
        return
    rendered = render_result_comment(state_name, result)
    comments = await tracker.fetch_issue_comments(issue.id)
    for comment in comments:
        parsed = _require_parsed_result_comment(comment, issue.id)
        if parsed is None or parsed[0] != state_name:
            continue
        if comment.id is None:
            raise RuntimeError(
                f"missing_state_result_comment_id issue_id={issue.id} state={state_name}"
            )
        await tracker.update_comment(comment.id, rendered)
        return
    await tracker.create_comment(issue.id, rendered)


async def build_prompt_issue_data(tracker: Tracker, issue: Issue) -> dict[str, Any]:
    """Build enriched prompt input including parsed upstream blocker results."""

    issue_data = asdict(issue)
    blocker_ids = [
        blocker.id
        for blocker in issue.blocked_by
        if isinstance(blocker.id, str) and blocker.id
    ]
    if not blocker_ids:
        issue_data["upstream_tickets"] = []
        return issue_data
    upstream_issues = await tracker.fetch_issue_states_by_ids(
        list(dict.fromkeys(blocker_ids))
    )
    upstream_tickets: list[dict[str, Any]] = []
    for upstream_issue in upstream_issues:
        if not upstream_issue.id:
            continue
        comments = await tracker.fetch_issue_comments(upstream_issue.id)
        upstream_tickets.append(
            {
                "id": upstream_issue.id,
                "identifier": upstream_issue.identifier,
                "title": upstream_issue.title,
                "state": upstream_issue.state,
                "url": upstream_issue.url,
                "results_by_state": parse_results_by_state(
                    comments,
                    ticket_label=upstream_issue.identifier or upstream_issue.id,
                ),
            }
        )
    issue_data["upstream_tickets"] = upstream_tickets
    return issue_data


def parse_results_by_state(
    comments: list[IssueComment], *, ticket_label: str
) -> dict[str, dict[str, Any]]:
    """Return parsed state results keyed by the human state name."""

    results_by_state: dict[str, dict[str, Any]] = {}
    for comment in comments:
        parsed = _require_parsed_result_comment(comment, ticket_label)
        if parsed is None:
            continue
        state_name, result = parsed
        results_by_state[state_name] = result.asdict()
    return results_by_state


def _require_parsed_result_comment(
    comment: IssueComment, ticket_label: str
) -> tuple[str, StructuredTurnResult] | None:
    parsed = parse_result_comment(comment.body)
    if parsed is not None:
        return parsed
    if isinstance(comment.body, str) and comment.body.startswith(RESULT_COMMENT_PREFIX):
        raise RuntimeError(
            f"malformed_state_result_comment ticket={ticket_label} comment_id={comment.id or 'n/a'}"
        )
    return None
