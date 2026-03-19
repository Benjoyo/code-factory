"""Helpers for rendering and installing the bundled workflow template."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from .loader import DEFAULT_WORKFLOW_FILENAME

_TOKEN_PREFIX = "[[CF_"
_TOKEN_SUFFIX = "]]"


@dataclass(frozen=True, slots=True)
class WorkflowTemplateValues:
    """Values injected into the starter workflow meta-template."""

    tracker_kind: str
    project_slug: str
    git_repo: str
    active_states: tuple[str, ...]
    terminal_states: tuple[str, ...]
    workspace_root: str
    max_concurrent_agents: int


def default_workflow_template() -> str:
    """Return the bundled workflow meta-template shipped with the package."""

    return (
        files("code_factory.workflow")
        .joinpath("templates", "default.md")
        .read_text(encoding="utf-8")
    )


def render_default_workflow(values: WorkflowTemplateValues) -> str:
    """Render the bundled workflow template with project-specific starter values."""

    rendered = default_workflow_template()
    replacements = {
        "TRACKER_KIND": yaml_string(values.tracker_kind),
        "PROJECT_SLUG": yaml_string(values.project_slug),
        "GIT_REPO": shlex.quote(values.git_repo),
        "STATE_PROFILES": yaml_state_profiles(values.active_states),
        "TERMINAL_STATES": yaml_list(values.terminal_states),
        "WORKSPACE_ROOT": yaml_string(values.workspace_root),
        "MAX_CONCURRENT_AGENTS": str(values.max_concurrent_agents),
    }
    for key, replacement in replacements.items():
        rendered = rendered.replace(token(key), replacement)
    return rendered


def initialize_workflow(
    destination: Path | None = None,
    *,
    values: WorkflowTemplateValues,
    force: bool = False,
) -> Path:
    """Write the rendered starter workflow to disk and return the target path."""

    target = destination or (Path.cwd() / DEFAULT_WORKFLOW_FILENAME)
    if target.exists() and not force:
        raise FileExistsError(str(target))
    target.write_text(render_default_workflow(values), encoding="utf-8")
    return target


def token(name: str) -> str:
    """Return the literal token name used inside the meta-template."""

    return f"{_TOKEN_PREFIX}{name}{_TOKEN_SUFFIX}"


def yaml_list(values: tuple[str, ...]) -> str:
    """Render a sequence as an indented YAML list fragment."""

    return "\n".join(f"    - {yaml_string(value)}" for value in values)


def yaml_state_profiles(values: tuple[str, ...]) -> str:
    """Render the starter state-profile mapping that points to one shared prompt."""

    return "\n".join(
        f"  {yaml_string(value)}:\n    prompt: default" for value in values
    )


def yaml_string(value: str) -> str:
    """Render a scalar string using JSON quoting, which YAML accepts."""

    return json.dumps(value)
