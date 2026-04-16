"""Support helpers for native merge orchestration."""

from __future__ import annotations

import json
from typing import Any

from ...workspace.review.review_resolution import ReviewError


async def capture_json(
    command: str, *, cwd: str, shell_capture, error_prefix: str
) -> Any:
    result = await shell_capture(command, cwd=cwd)
    if result.status != 0:
        raise ReviewError(result.output or error_prefix)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise ReviewError(f"{error_prefix}: invalid JSON") from exc


def require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    raise ReviewError(f"GitHub CLI PR payload is missing `{key}`.")


def require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    raise ReviewError(f"GitHub CLI PR payload is missing `{key}`.")


def optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def is_not_found_error(error: ReviewError) -> bool:
    message = str(error)
    return '"status": "404"' in message or "HTTP 404" in message


def pull_request_check_run_repositories(payload: Any) -> tuple[str, ...]:
    head = payload.get("head") if isinstance(payload, dict) else None
    base = payload.get("base") if isinstance(payload, dict) else None
    paths = (
        repository_full_name(head),
        repository_full_name(base),
    )
    return tuple(dict.fromkeys(path for path in paths if path is not None))


def repository_full_name(ref_payload: Any) -> str | None:
    repo = ref_payload.get("repo") if isinstance(ref_payload, dict) else None
    full_name = repo.get("full_name") if isinstance(repo, dict) else None
    return full_name if isinstance(full_name, str) and full_name.strip() else None
