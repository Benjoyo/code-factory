"""Shared workspace-local workpad path and content helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

WORKPAD_FILENAME = "workpad.md"


def workspace_workpad_path(workspace: str) -> str:
    """Return the canonical workspace-local workpad file path."""

    return str(Path(workspace) / WORKPAD_FILENAME)


def workpad_content_hash(path: str) -> str | None:
    """Return a stable hash of the current workpad contents when the file exists."""

    workpad = Path(path)
    if not workpad.is_file():
        return None
    return hashlib.sha256(workpad.read_bytes()).hexdigest()
