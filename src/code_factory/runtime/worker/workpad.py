"""Helpers for hydrating and persisting the workspace-local workpad file."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ...config.models import Settings
from ...issues import Issue, IssueComment
from ...trackers import build_tracker_ops
from ...trackers.base import Tracker
from ...workspace.workpad import WORKPAD_FILENAME, workspace_workpad_path

WORKPAD_HEADER = "## Codex Workpad"
_SYNC_LOCKS: dict[str, asyncio.Lock] = {}
DEFAULT_WORKPAD_BODY = """## Codex Workpad

### Plan

### Acceptance Criteria

### QA Plan

### Validation

### Notes

### Handoff
"""


async def hydrate_workspace_workpad(
    settings: Settings, tracker: Tracker, issue: Issue, workspace: str
) -> str:
    """Write the current tracker workpad into the workspace-local workpad file."""

    body = await _load_workpad_body(settings, tracker, issue, workspace)
    path = workspace_workpad_path(workspace)
    workpad_path = Path(path)
    workpad_path.parent.mkdir(parents=True, exist_ok=True)
    workpad_path.write_text(body or DEFAULT_WORKPAD_BODY, encoding="utf-8")
    return path


async def sync_workspace_workpad(
    settings: Settings, tracker: Tracker, issue: Issue, workspace: str
) -> None:
    """Persist the workspace-local workpad file back to the tracker."""

    async with _sync_lock(workspace):
        if settings.tracker.kind == "linear" and issue.identifier:
            tracker_ops = build_tracker_ops(settings, allowed_roots=(workspace,))
            try:
                await tracker_ops.sync_workpad(
                    issue.identifier, file_path=WORKPAD_FILENAME
                )
            finally:
                await tracker_ops.close()
            return
        if not issue.id:
            raise RuntimeError("missing_issue_id_for_workpad_sync")
        body = Path(workspace_workpad_path(workspace)).read_text(encoding="utf-8")
        existing = await _fallback_workpad_comment(tracker, issue)
        if existing is None:
            await tracker.create_comment(issue.id, body)
            return
        if existing.id is None:
            raise RuntimeError("missing_workpad_comment_id")
        await tracker.update_comment(existing.id, body)


async def _load_workpad_body(
    settings: Settings, tracker: Tracker, issue: Issue, workspace: str
) -> str | None:
    if settings.tracker.kind == "linear" and issue.identifier:
        tracker_ops = build_tracker_ops(settings, allowed_roots=(workspace,))
        try:
            payload = await tracker_ops.get_workpad(issue.identifier)
        finally:
            await tracker_ops.close()
        body = payload.get("body") if isinstance(payload, dict) else None
        if isinstance(body, str) and body.strip():
            return body
        return None
    comment = await _fallback_workpad_comment(tracker, issue)
    body = comment.body if comment is not None else None
    return body if isinstance(body, str) and body.strip() else None


async def _fallback_workpad_comment(
    tracker: Tracker, issue: Issue
) -> IssueComment | None:
    if not issue.id:
        return None
    comments = await tracker.fetch_issue_comments(issue.id)
    for comment in reversed(comments):
        if isinstance(comment.body, str) and comment.body.startswith(WORKPAD_HEADER):
            return comment
    return None


def _sync_lock(workspace: str) -> asyncio.Lock:
    lock = _SYNC_LOCKS.get(workspace)
    if lock is None:
        lock = asyncio.Lock()
        _SYNC_LOCKS[workspace] = lock
    return lock
