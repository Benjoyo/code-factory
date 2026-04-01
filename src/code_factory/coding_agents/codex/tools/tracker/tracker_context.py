"""Helpers for resolving current issue/project defaults for dynamic tools."""

from __future__ import annotations

from .....errors import TrackerClientError
from ..registry import ToolContext


def resolve_issue(context: ToolContext, issue: str | None) -> str:
    """Return an explicit issue identifier or the current turn's issue."""

    resolved = issue or context.current_issue
    if isinstance(resolved, str) and resolved.strip():
        return resolved.strip()
    raise TrackerClientError(("tracker_missing_field", "`issue` is required"))


def resolve_project(context: ToolContext) -> str:
    """Return the current turn's workflow project slug."""

    project = context.current_project
    if isinstance(project, str) and project.strip():
        return project.strip()
    raise TrackerClientError(("tracker_missing_field", "`project` is required"))
