from __future__ import annotations

import os
import re
from pathlib import Path

from ..errors import WorkspaceError

SAFE_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9._-]")


def canonicalize(path: str) -> str:
    expanded = os.path.abspath(os.path.expanduser(path))
    return os.path.normpath(_resolve_segments(expanded))


def is_within(root: str, candidate: str) -> bool:
    root_path = Path(root)
    candidate_path = Path(candidate)
    try:
        candidate_path.relative_to(root_path)
        return True
    except ValueError:
        return False


def safe_identifier(identifier: str | None) -> str:
    return SAFE_IDENTIFIER_RE.sub("_", identifier or "issue")


def workspace_path_for_issue(root: str, identifier: str | None) -> str:
    return canonicalize(os.path.join(root, safe_identifier(identifier)))


def validate_workspace_path(root: str, workspace: str) -> str:
    expanded_workspace = os.path.abspath(os.path.expanduser(workspace))
    expanded_root = os.path.abspath(os.path.expanduser(root))

    try:
        canonical_workspace = canonicalize(expanded_workspace)
        canonical_root = canonicalize(expanded_root)
    except OSError as exc:
        raise WorkspaceError(
            ("workspace_path_unreadable", workspace, str(exc))
        ) from exc

    if canonical_workspace == canonical_root:
        raise WorkspaceError(
            ("workspace_equals_root", canonical_workspace, canonical_root)
        )
    if (canonical_workspace + os.sep).startswith(canonical_root + os.sep):
        return canonical_workspace
    if (expanded_workspace + os.sep).startswith(expanded_root + os.sep):
        raise WorkspaceError(
            ("workspace_symlink_escape", expanded_workspace, canonical_root)
        )
    raise WorkspaceError(
        ("workspace_outside_root", canonical_workspace, canonical_root)
    )


def _resolve_segments(path: str) -> str:
    drive, tail = os.path.splitdrive(path)
    if os.name == "nt":  # pragma: no cover - Windows-only drive handling
        resolved_root = drive + os.sep if tail.startswith(os.sep) else drive
        raw_segments = [segment for segment in tail.split(os.sep) if segment]
    else:
        resolved_root = os.sep
        raw_segments = [segment for segment in path.split(os.sep) if segment]

    resolved_segments: list[str] = []
    index = 0
    while index < len(raw_segments):
        segment = raw_segments[index]
        candidate = _join_path(resolved_root or os.sep, resolved_segments + [segment])
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            return _join_path(
                resolved_root or os.sep, resolved_segments + raw_segments[index:]
            )

        if os.path.islink(candidate):
            resolved_root, raw_segments = _resolve_symlink(
                candidate, resolved_root, resolved_segments, raw_segments, index
            )
            resolved_segments = []
            index = 0
            continue

        resolved_segments.append(segment)
        index += 1

    return _join_path(resolved_root or os.sep, resolved_segments)


def _resolve_symlink(
    candidate: str,
    resolved_root: str,
    resolved_segments: list[str],
    raw_segments: list[str],
    index: int,
) -> tuple[str, list[str]]:
    target = os.readlink(candidate)
    base_dir = _join_path(resolved_root or os.sep, resolved_segments)
    resolved_target = os.path.abspath(os.path.join(base_dir, target))
    drive, tail = os.path.splitdrive(resolved_target)
    if os.name == "nt":  # pragma: no cover - Windows-only drive handling
        next_root = drive + os.sep if tail.startswith(os.sep) else drive
        next_segments = [segment for segment in tail.split(os.sep) if segment]
    else:
        next_root = os.sep
        next_segments = [
            segment for segment in resolved_target.split(os.sep) if segment
        ]
    return next_root, next_segments + raw_segments[index + 1 :]


def _join_path(root: str, segments: list[str]) -> str:
    path = root
    for segment in segments:
        path = os.path.join(path, segment)
    return path
