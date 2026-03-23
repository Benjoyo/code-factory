"""Helpers for discovering the bound local control-plane endpoint."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def runtime_metadata_path(workflow_path: str) -> Path:
    digest = hashlib.sha256(workflow_path.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / "code-factory-runtime" / f"{digest}.json"


def read_runtime_metadata(workflow_path: str) -> dict[str, Any] | None:
    path = runtime_metadata_path(workflow_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_runtime_metadata(
    workflow_path: str,
    *,
    host: str,
    port: int,
    pid: int | None,
) -> Path:
    path = runtime_metadata_path(workflow_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workflow_path": workflow_path,
                "host": host,
                "port": port,
                "pid": pid,
                "started_at": datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def clear_runtime_metadata(workflow_path: str) -> None:
    path = runtime_metadata_path(workflow_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
