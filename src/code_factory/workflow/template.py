"""Helpers for loading and installing the bundled default workflow template."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .loader import DEFAULT_WORKFLOW_FILENAME


def default_workflow_template() -> str:
    """Return the bundled starter workflow shipped with the package."""

    return (
        files("code_factory.workflow")
        .joinpath("templates", "default.md")
        .read_text(encoding="utf-8")
    )


def initialize_workflow(
    destination: Path | None = None, *, force: bool = False
) -> Path:
    """Write the bundled workflow template to disk and return the target path."""

    target = destination or (Path.cwd() / DEFAULT_WORKFLOW_FILENAME)
    if target.exists() and not force:
        raise FileExistsError(str(target))
    target.write_text(default_workflow_template(), encoding="utf-8")
    return target
