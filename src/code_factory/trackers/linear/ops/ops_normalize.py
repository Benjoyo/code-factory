"""Normalization helpers for Linear ticket operation payloads."""

from __future__ import annotations

from typing import Any


def normalize_state(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "type": node.get("type"),
    }


def normalize_comment(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "body": node.get("body"),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "resolved_at": node.get("resolvedAt"),
        "user_name": ((node.get("user") or {}) if isinstance(node, dict) else {}).get(
            "name"
        ),
    }


def normalize_attachment(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "title": node.get("title"),
        "subtitle": node.get("subtitle"),
        "url": node.get("url"),
        "source_type": node.get("sourceType"),
        "metadata": node.get("metadata"),
    }


def normalize_relation(node: dict[str, Any], issue_key: str) -> dict[str, Any]:
    related = node.get(issue_key) or {}
    return {
        "type": node.get("type"),
        "issue": {
            "id": related.get("id"),
            "identifier": related.get("identifier"),
            "title": related.get("title"),
            "state": normalize_state(related.get("state")),
        },
    }


def normalize_team(
    node: dict[str, Any] | None, *, include_states: bool
) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    team = {
        "id": node.get("id"),
        "name": node.get("name"),
        "key": node.get("key"),
    }
    if include_states:
        team["states"] = [
            normalize_state(state)
            for state in ((node.get("states") or {}).get("nodes") or [])
            if isinstance(state, dict)
        ]
    return team


def normalize_project(
    node: dict[str, Any] | None, *, include_teams: bool
) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    project = {
        "id": node.get("id"),
        "name": node.get("name"),
        "url": node.get("url"),
    }
    if include_teams:
        project["teams"] = [
            normalize_team(team, include_states=True)
            for team in ((node.get("teams") or {}).get("nodes") or [])
            if isinstance(team, dict)
        ]
    return project


def normalize_issue(
    node: dict[str, Any],
    *,
    include_description: bool,
    include_comments: bool,
    include_attachments: bool,
    include_relations: bool,
) -> dict[str, Any]:
    issue = {
        "id": node.get("id"),
        "identifier": node.get("identifier"),
        "title": node.get("title"),
        "priority": node.get("priority"),
        "url": node.get("url"),
        "branch_name": node.get("branchName"),
        "state": normalize_state(node.get("state")),
        "project": normalize_project(node.get("project"), include_teams=False),
        "team": normalize_team(node.get("team"), include_states=False),
        "labels": [
            label.get("name")
            for label in ((node.get("labels") or {}).get("nodes") or [])
            if isinstance(label, dict) and label.get("name")
        ],
    }
    if include_description:
        issue["description"] = node.get("description")
    if include_comments:
        issue["comments"] = [
            normalize_comment(comment)
            for comment in ((node.get("comments") or {}).get("nodes") or [])
            if isinstance(comment, dict)
        ]
    if include_attachments:
        issue["attachments"] = [
            normalize_attachment(attachment)
            for attachment in ((node.get("attachments") or {}).get("nodes") or [])
            if isinstance(attachment, dict)
        ]
    if include_relations:
        issue["relations"] = {
            "blocked_by": [
                normalize_relation(relation, "issue")
                for relation in (
                    (node.get("inverseRelations") or {}).get("nodes") or []
                )
                if isinstance(relation, dict) and relation.get("type") == "blocks"
            ],
            "blocks": [
                normalize_relation(relation, "relatedIssue")
                for relation in ((node.get("relations") or {}).get("nodes") or [])
                if isinstance(relation, dict) and relation.get("type") == "blocks"
            ],
            "related": [
                normalize_relation(relation, "issue")
                for relation in (
                    (node.get("inverseRelations") or {}).get("nodes") or []
                )
                if isinstance(relation, dict) and relation.get("type") == "related"
            ]
            + [
                normalize_relation(relation, "relatedIssue")
                for relation in ((node.get("relations") or {}).get("nodes") or [])
                if isinstance(relation, dict) and relation.get("type") == "related"
            ],
        }
    return issue
