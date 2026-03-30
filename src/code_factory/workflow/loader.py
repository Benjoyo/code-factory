"""Workflow file loading and front-matter parsing helpers."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import yaml

from ..config.utils import configured_active_states
from ..errors import WorkflowLoadError
from .models import FileStamp, WorkflowDefinition

DEFAULT_WORKFLOW_FILENAME = "WORKFLOW.md"
SECTION_HEADING = re.compile(r"^#\s+(prompt|review):\s*(.+?)\s*$")


def workflow_file_path(selected_path: str | None = None) -> str:
    """Return the workflow path, defaulting to `WORKFLOW.md` in the current cwd."""

    if selected_path:
        return selected_path
    return os.path.join(os.getcwd(), DEFAULT_WORKFLOW_FILENAME)


def load_workflow(path: str) -> WorkflowDefinition:
    """Read and validate a runnable workflow file."""

    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowLoadError(
            ("missing_workflow_file", path, exc.strerror or type(exc).__name__)
        ) from exc
    definition = parse_workflow(content)
    _validate_runnable_workflow(definition)
    return definition


def parse_workflow(content: str) -> WorkflowDefinition:
    """Split a workflow document into config front matter and prompt template."""

    front_matter_lines, prompt_lines = split_front_matter(content)
    config = front_matter_yaml_to_map(front_matter_lines)
    prompt = "\n".join(prompt_lines).strip()
    prompt_sections: dict[str, str] = {}
    review_sections: dict[str, str] = {}
    if "states" in config:
        prompt_sections, review_sections = parse_named_sections(prompt_lines)
    return WorkflowDefinition(
        config=config,
        prompt_template="" if prompt_sections else prompt,
        prompt_sections=prompt_sections,
        review_sections=review_sections,
    )


def _validate_runnable_workflow(definition: WorkflowDefinition) -> None:
    """Reject documents that parse but cannot satisfy the runtime contract."""

    configured_active_states(definition.config, {})


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


def parse_prompt_sections(lines: list[str]) -> dict[str, str]:
    """Parse named prompt sections for multi-state workflows."""

    prompt_sections, _review_sections = parse_named_sections(lines)
    return prompt_sections


def parse_named_sections(lines: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Parse named prompt and review sections from the workflow body."""

    sections = {"prompt": {}, "review": {}}
    current_kind: str | None = None
    current_name: str | None = None
    current_lines: list[str] = []
    for line in lines:
        section_match = SECTION_HEADING.match(line)
        if section_match is not None:
            current_kind, current_name = finalize_named_section(
                sections, current_kind, current_name, current_lines
            )
            current_lines = []
            section_kind = section_match.group(1).strip()
            section_name = section_match.group(2).strip()
            if not section_name:
                raise WorkflowLoadError(f"workflow_{section_kind}_section_name_blank")
            if section_name in sections[section_kind] or (
                section_kind == current_kind and section_name == current_name
            ):
                raise WorkflowLoadError(
                    (f"workflow_{section_kind}_section_duplicate", section_name)
                )
            current_kind = section_kind
            current_name = section_name
            continue
        if current_name is None:
            if line.strip():
                raise WorkflowLoadError("workflow_prompt_section_stray_content")
            continue
        current_lines.append(line)

    finalize_named_section(sections, current_kind, current_name, current_lines)
    if not sections["prompt"]:
        raise WorkflowLoadError("workflow_prompt_sections_missing")
    return sections["prompt"], sections["review"]


def finalize_prompt_section(
    sections: dict[str, str], current_name: str | None, current_lines: list[str]
) -> str | None:
    """Write the current prompt section back into the parsed section map."""

    if current_name is None:
        return None
    sections[current_name] = "\n".join(current_lines).strip()
    return None


def finalize_named_section(
    sections: dict[str, dict[str, str]],
    current_kind: str | None,
    current_name: str | None,
    current_lines: list[str],
) -> tuple[str | None, str | None]:
    """Write the current named section back into the parsed section map."""

    if current_name is None:
        return None, None
    assert current_kind is not None
    sections[current_kind][current_name] = "\n".join(current_lines).strip()
    return None, None


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
