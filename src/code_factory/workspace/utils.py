from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..issues import Issue

EXCLUDED_ENTRIES = {".elixir_ls", "tmp"}


def issue_context(issue_or_identifier: Issue | str | None) -> dict[str, str | None]:
    if isinstance(issue_or_identifier, Issue):
        return {
            "issue_id": issue_or_identifier.id,
            "issue_identifier": issue_or_identifier.identifier or "issue",
        }
    if isinstance(issue_or_identifier, str):
        return {"issue_id": None, "issue_identifier": issue_or_identifier}
    return {"issue_id": None, "issue_identifier": "issue"}


def ensure_workspace(path: str) -> bool:
    if os.path.isdir(path):
        clean_tmp_artifacts(path)
        return False
    if os.path.exists(path):
        if os.path.isdir(
            path
        ):  # pragma: no cover - filesystem can change between exists/isdir checks
            shutil.rmtree(path)
        else:
            os.unlink(path)
    Path(path).mkdir(parents=True, exist_ok=True)
    return True


def clean_tmp_artifacts(path: str) -> None:
    for entry in EXCLUDED_ENTRIES:
        entry_path = os.path.join(path, entry)
        if os.path.exists(entry_path):
            shutil.rmtree(entry_path, ignore_errors=True)
