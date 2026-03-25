"""Resolution helpers for Linear ticket operations."""

from __future__ import annotations

from typing import Any

from ...errors import TrackerClientError


def matches_identity(node: dict[str, Any], needle: str, *keys: str) -> bool:
    lowered = needle.strip().lower()
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip().lower() == lowered:
            return True
    return False


def find_exact(nodes: list[dict[str, Any]], needle: str, *keys: str) -> dict[str, Any]:
    for node in nodes:
        if matches_identity(node, needle, *keys):
            return node
    raise TrackerClientError(("tracker_not_found", needle))


def find_optional(
    nodes: list[dict[str, Any]], needle: str, *keys: str
) -> dict[str, Any] | None:
    for node in nodes:
        if matches_identity(node, needle, *keys):
            return node
    return None


def require_single(
    nodes: list[dict[str, Any]], label: str, *, field_name: str
) -> dict[str, Any]:
    if len(nodes) == 1:
        return nodes[0]
    if not nodes:
        raise TrackerClientError(
            ("tracker_missing_field", f"`{field_name}` is required")
        )
    raise TrackerClientError(
        (
            "tracker_ambiguous",
            f"`{field_name}` is required because {label} has multiple teams",
        )
    )
