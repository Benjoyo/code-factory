from __future__ import annotations

"""Sandbox and workspace-policy helpers for Codex app-server sessions."""

import os
from typing import Any

from ....config.models import CodingAgentSettings
from ....errors import AppServerError, WorkspaceError
from ....workspace.paths import canonicalize, validate_workspace_path


def validate_workspace_cwd(workspace_root: str, workspace: str) -> str:
    """Check the workspace path is inside the configured workspace root."""

    expanded_workspace = os.path.abspath(os.path.expanduser(workspace))
    expanded_root = os.path.abspath(os.path.expanduser(workspace_root))
    canonical_workspace = canonicalize(expanded_workspace)
    canonical_root = canonicalize(expanded_root)
    try:
        return validate_workspace_path(canonical_root, canonical_workspace)
    except WorkspaceError as exc:
        reason = exc.reason if isinstance(exc.reason, tuple) else (exc.reason,)
        raise AppServerError(("invalid_workspace_cwd", *reason)) from exc


def resolve_turn_sandbox_policy(
    coding_agent: CodingAgentSettings,
    workspace_root: str,
    workspace: str,
) -> dict[str, Any]:
    """Return the configured per-turn sandbox policy."""

    if coding_agent.turn_sandbox_policy is not None:
        return coding_agent.turn_sandbox_policy
    writable_root = canonicalize(workspace or workspace_root)
    return {
        "type": "workspaceWrite",
        "writableRoots": [writable_root],
        "readOnlyAccess": {"type": "fullAccess"},
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def review_turn_sandbox_policy(workspace_root: str, workspace: str) -> dict[str, Any]:
    """Return the read-only sandbox policy used for review turns."""

    readable_root = canonicalize(workspace or workspace_root)
    return {
        "type": "readOnly",
        "networkAccess": False,
        "access": {
            "type": "restricted",
            "includePlatformDefaults": True,
            "readableRoots": [readable_root],
        },
    }
