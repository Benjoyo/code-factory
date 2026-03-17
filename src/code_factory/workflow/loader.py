"""Workflow file loading and front-matter parsing helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

from ..errors import WorkflowLoadError
from .models import FileStamp, WorkflowDefinition

DEFAULT_WORKFLOW_FILENAME = "WORKFLOW.md"


def workflow_file_path(selected_path: str | None = None) -> str:
    """Return the workflow path, defaulting to `WORKFLOW.md` in the current cwd."""

    if selected_path:
        return selected_path
    return os.path.join(os.getcwd(), DEFAULT_WORKFLOW_FILENAME)


def load_workflow(path: str) -> WorkflowDefinition:
    """Read and parse a workflow file while normalizing missing-file errors."""

    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowLoadError(
            ("missing_workflow_file", path, exc.strerror or type(exc).__name__)
        ) from exc
    return parse_workflow(content)


def parse_workflow(content: str) -> WorkflowDefinition:
    """Split the markdown document into config front matter and prompt template."""

    front_matter_lines, prompt_lines = split_front_matter(content)
    prompt = "\n".join(prompt_lines).strip()
    return WorkflowDefinition(
        config=front_matter_yaml_to_map(front_matter_lines),
        prompt_template=prompt,
    )


def split_front_matter(content: str) -> tuple[list[str], list[str]]:
    """Separate YAML front matter from the prompt body if the file starts with `---`."""

    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return [], lines

    front_matter: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            return front_matter, lines[index + 1 :]
        front_matter.append(line)
    return front_matter, []


def front_matter_yaml_to_map(lines: list[str]) -> dict[str, Any]:
    """Decode front matter into a mapping and reject non-object YAML payloads."""

    yaml_source = "\n".join(lines)
    if not yaml_source.strip():
        return {}

    try:
        decoded = yaml.safe_load(yaml_source)
    except yaml.YAMLError as exc:
        raise WorkflowLoadError(("workflow_parse_error", str(exc))) from exc

    if decoded is None:
        return {}
    if not isinstance(decoded, dict):
        raise WorkflowLoadError("workflow_front_matter_not_a_map")
    return decoded


def current_stamp(path: str) -> FileStamp:
    """Capture a cheap change stamp so reloads can detect workflow updates."""

    try:
        stat = os.stat(path)
        content = Path(path).read_bytes()
    except OSError as exc:
        raise WorkflowLoadError(
            ("missing_workflow_file", path, exc.strerror or type(exc).__name__)
        ) from exc

    return FileStamp(
        mtime=int(stat.st_mtime_ns),
        size=stat.st_size,
        digest=hashlib.sha256(content).hexdigest(),
    )
