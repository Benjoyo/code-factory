"""Workspace-bounded file helpers for ticket operations."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from ....errors import TrackerClientError
from ....workspace.paths import canonicalize, is_within


def resolve_path(path: str, allowed_roots: tuple[str, ...]) -> str:
    file_path = Path(path)
    if not file_path.is_absolute() and allowed_roots:
        file_path = Path(allowed_roots[0]) / file_path
    try:
        canonical_file = canonicalize(str(file_path))
    except OSError as exc:
        raise TrackerClientError(
            ("tracker_file_error", f"cannot read `{path}`: {exc}")
        ) from exc
    if allowed_roots and not any(
        is_within(root, canonical_file) for root in allowed_roots
    ):
        raise TrackerClientError(
            ("tracker_file_error", f"`{path}` is outside the allowed workspace roots")
        )
    return canonical_file


def read_text_file(path: str, allowed_roots: tuple[str, ...]) -> str:
    canonical_file = resolve_path(path, allowed_roots)
    try:
        body = Path(canonical_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise TrackerClientError(
            ("tracker_file_error", f"cannot read `{path}`: {exc}")
        ) from exc
    if body == "":
        raise TrackerClientError(("tracker_file_error", f"file is empty: `{path}`"))
    return body


def read_binary_file(
    path: str, allowed_roots: tuple[str, ...]
) -> tuple[str, bytes, str]:
    canonical_file = resolve_path(path, allowed_roots)
    try:
        content = Path(canonical_file).read_bytes()
    except OSError as exc:
        raise TrackerClientError(
            ("tracker_file_error", f"cannot read `{path}`: {exc}")
        ) from exc
    if not content:
        raise TrackerClientError(("tracker_file_error", f"file is empty: `{path}`"))
    content_type = mimetypes.guess_type(canonical_file)[0] or "application/octet-stream"
    return Path(canonical_file).name, content, content_type
