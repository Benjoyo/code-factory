from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ...errors import TrackerClientError
from ...issues import BlockerRef, Issue


def decode_linear_page_response(
    body: dict[str, Any],
    assignee_filter: dict[str, Any] | None,
) -> tuple[list[Issue], dict[str, Any]]:
    issues_payload = body.get("data", {}).get("issues")
    if not isinstance(issues_payload, Mapping):
        if body.get("errors") is not None:
            raise TrackerClientError(("linear_graphql_errors", body["errors"]))
        raise TrackerClientError("linear_unknown_payload")

    nodes = issues_payload.get("nodes")
    page_info = issues_payload.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, Mapping):
        raise TrackerClientError("linear_unknown_payload")

    return decode_nodes(nodes, assignee_filter), {
        "has_next_page": page_info.get("hasNextPage") is True,
        "end_cursor": page_info.get("endCursor"),
    }


def decode_linear_response(
    body: dict[str, Any], assignee_filter: dict[str, Any] | None
) -> list[Issue]:
    issues_payload = body.get("data", {}).get("issues")
    if isinstance(issues_payload, Mapping) and isinstance(
        issues_payload.get("nodes"), list
    ):
        return decode_nodes(issues_payload["nodes"], assignee_filter)
    if body.get("errors") is not None:
        raise TrackerClientError(("linear_graphql_errors", body["errors"]))
    raise TrackerClientError("linear_unknown_payload")


def decode_nodes(
    nodes: list[Any], assignee_filter: dict[str, Any] | None
) -> list[Issue]:
    issues: list[Issue] = []
    for node in nodes:
        issue = normalize_issue(node, assignee_filter)
        if issue is not None:
            issues.append(issue)
    return issues


def normalize_issue(
    issue: Mapping[str, Any], assignee_filter: dict[str, Any] | None
) -> Issue | None:
    if not isinstance(issue, Mapping):
        return None
    assignee = issue.get("assignee")
    return Issue(
        id=string_or_none(issue.get("id")),
        identifier=string_or_none(issue.get("identifier")),
        title=string_or_none(issue.get("title")),
        description=string_or_none(issue.get("description")),
        priority=issue.get("priority")
        if isinstance(issue.get("priority"), int)
        else None,
        state=string_or_none(
            (issue.get("state") or {}).get("name")
            if isinstance(issue.get("state"), Mapping)
            else None
        ),
        branch_name=string_or_none(issue.get("branchName")),
        url=string_or_none(issue.get("url")),
        assignee_id=assignee_id(assignee),
        blocked_by=tuple(extract_blockers(issue)),
        labels=tuple(extract_labels(issue)),
        assigned_to_worker=assigned_to_worker(assignee, assignee_filter),
        created_at=parse_datetime(issue.get("createdAt")),
        updated_at=parse_datetime(issue.get("updatedAt")),
    )


def assigned_to_worker(assignee: Any, assignee_filter: dict[str, Any] | None) -> bool:
    if assignee_filter is None:
        return True
    current_assignee_id = assignee_id(assignee)
    return (
        current_assignee_id in assignee_filter["match_values"]
        if current_assignee_id is not None
        else False
    )


def assignee_id(assignee: Any) -> str | None:
    if isinstance(assignee, Mapping):
        raw = assignee.get("id")
        if isinstance(raw, str):
            stripped = raw.strip()
            return stripped or None
    return None


def extract_labels(issue: Mapping[str, Any]) -> list[str]:
    labels = issue.get("labels", {})
    nodes = labels.get("nodes") if isinstance(labels, Mapping) else None
    if not isinstance(nodes, list):
        return []
    return [
        node["name"].lower()
        for node in nodes
        if isinstance(node, Mapping) and isinstance(node.get("name"), str)
    ]


def extract_blockers(issue: Mapping[str, Any]) -> list[BlockerRef]:
    inverse_relations = issue.get("inverseRelations", {})
    nodes = (
        inverse_relations.get("nodes")
        if isinstance(inverse_relations, Mapping)
        else None
    )
    if not isinstance(nodes, list):
        return []

    blockers: list[BlockerRef] = []
    for relation in nodes:
        if (
            not isinstance(relation, Mapping)
            or str(relation.get("type", "")).strip().lower() != "blocks"
        ):
            continue
        blocker_issue = relation.get("issue")
        if not isinstance(blocker_issue, Mapping):
            continue
        blocker_state = blocker_issue.get("state")
        blockers.append(
            BlockerRef(
                id=string_or_none(blocker_issue.get("id")),
                identifier=string_or_none(blocker_issue.get("identifier")),
                state=string_or_none(
                    blocker_state.get("name")
                    if isinstance(blocker_state, Mapping)
                    else None
                ),
            )
        )
    return blockers


def next_page_cursor(page_info: Mapping[str, Any]) -> str | None:
    if page_info.get("has_next_page") is not True:
        return None
    cursor = page_info.get("end_cursor")
    if not isinstance(cursor, str) or not cursor:
        raise TrackerClientError("linear_missing_end_cursor")
    return cursor


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
