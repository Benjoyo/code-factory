"""Shared workspace-local workpad path helpers."""

from __future__ import annotations

from pathlib import Path

WORKPAD_FILENAME = "workpad.md"


def workspace_workpad_path(workspace: str) -> str:
    """Return the canonical workspace-local workpad file path."""

    return str(Path(workspace) / WORKPAD_FILENAME)
