"""Helpers for resolving persistent log locations from workflow settings."""

from __future__ import annotations

from pathlib import Path


def resolve_logs_root(
    workflow_path: str,
    *,
    override: str | None,
    file_logging_enabled: bool,
    configured_root: str | None,
) -> str | None:
    """Return the effective logs root for the current run."""

    if override is not None:
        return override
    if not file_logging_enabled:
        return None
    if configured_root is None:
        return str(_workflow_dir(workflow_path))
    return str(_resolve_configured_root(workflow_path, configured_root))


def _workflow_dir(workflow_path: str) -> Path:
    return Path(workflow_path).expanduser().resolve().parent


def _resolve_configured_root(workflow_path: str, configured_root: str) -> Path:
    candidate = Path(configured_root).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (_workflow_dir(workflow_path) / candidate).resolve()
