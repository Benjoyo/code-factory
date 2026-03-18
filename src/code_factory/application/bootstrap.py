"""Interactive project bootstrap helpers used by `cf init`."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from ..config.defaults import DEFAULT_WORKSPACE_ROOT
from ..workflow.loader import DEFAULT_WORKFLOW_FILENAME
from ..workflow.template import WorkflowTemplateValues, initialize_workflow

DEFAULT_TRACKER_KIND = "linear"
DEFAULT_ACTIVE_STATES = ("Todo", "In Progress", "Merging", "Rework")
DEFAULT_TERMINAL_STATES = ("Canceled", "Duplicate", "Done")
DEFAULT_MAX_CONCURRENT_AGENTS = 2


@dataclass(frozen=True, slots=True)
class ProjectInitResult:
    """Created paths returned by the project bootstrap flow."""

    workflow_path: Path
    skills_path: Path


def prompt_project_init(
    *, console: Console | None = None, target_dir: Path | None = None
) -> WorkflowTemplateValues:
    """Collect starter workflow values from the user via Rich prompts."""

    resolved_target = target_dir or Path.cwd()
    prompt_console = console or Console()
    prompt_console.print(
        Panel.fit(
            "Create a starter Code Factory workflow and copy the required skills "
            "into the current project.",
            title="cf init",
            border_style="cyan",
        )
    )
    tracker_kind = Prompt.ask(
        "Tracker kind",
        default=DEFAULT_TRACKER_KIND,
        console=prompt_console,
    ).strip()
    project_slug = prompt_non_empty(
        "Project slug",
        console=prompt_console,
        default=default_project_slug(resolved_target),
    )
    git_repo = prompt_non_empty(
        "Git repository",
        console=prompt_console,
        default=detect_git_repo(resolved_target),
    )
    active_states = prompt_state_list(
        prompt_console,
        label="Active states",
        defaults=DEFAULT_ACTIVE_STATES,
    )
    terminal_states = prompt_state_list(
        prompt_console,
        label="Terminal states",
        defaults=DEFAULT_TERMINAL_STATES,
    )
    workspace_root = prompt_non_empty(
        "Workspace root",
        console=prompt_console,
        default=DEFAULT_WORKSPACE_ROOT,
    )
    max_concurrent_agents = prompt_positive_int(
        "Max concurrent agents",
        console=prompt_console,
        default=DEFAULT_MAX_CONCURRENT_AGENTS,
    )
    return WorkflowTemplateValues(
        tracker_kind=tracker_kind,
        project_slug=project_slug,
        git_repo=git_repo,
        active_states=active_states,
        terminal_states=terminal_states,
        workspace_root=workspace_root,
        max_concurrent_agents=max_concurrent_agents,
    )


def initialize_project(
    values: WorkflowTemplateValues,
    *,
    target_dir: Path | None = None,
    force: bool = False,
) -> ProjectInitResult:
    """Render the starter workflow and copy bundled skills into the project."""

    resolved_target = target_dir or Path.cwd()
    workflow_path = resolved_target / DEFAULT_WORKFLOW_FILENAME
    skills_path = resolved_target / ".agents" / "skills"

    if workflow_path.exists() and not force:
        raise FileExistsError(str(workflow_path))
    if skills_path.exists() and not force:
        raise FileExistsError(str(skills_path))

    if force:
        remove_existing_path(skills_path)

    written_workflow = initialize_workflow(
        workflow_path,
        values=values,
        force=force,
    )
    copy_bootstrap_skills(skills_path)
    return ProjectInitResult(workflow_path=written_workflow, skills_path=skills_path)


def copy_bootstrap_skills(destination: Path) -> None:
    """Copy the packaged skill directories into the target project."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    source = resources.files("code_factory.application").joinpath(
        "bootstrap_assets", "skills"
    )
    copy_resource_tree(source, destination)


def copy_resource_tree(source: Traversable, destination: Path) -> None:
    """Copy an importlib resource tree to a normal filesystem destination."""

    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / target_name(child.name)
        if child.is_dir():
            copy_resource_tree(child, target)
            continue
        target.write_bytes(child.read_bytes())


def prompt_non_empty(
    label: str,
    *,
    console: Console,
    default: str | None = None,
) -> str:
    """Prompt for a non-empty string, re-asking until one is provided."""

    while True:
        response = Prompt.ask(label, default=default, console=console)
        value = "" if response is None else response.strip()
        if value:
            return value
        console.print(f"[red]{label} cannot be empty.[/red]")


def prompt_positive_int(
    label: str,
    *,
    console: Console,
    default: int,
) -> int:
    """Prompt for a strictly positive integer value."""

    while True:
        value = IntPrompt.ask(label, default=default, console=console)
        if value > 0:
            return value
        console.print(f"[red]{label} must be greater than zero.[/red]")


def prompt_state_list(
    console: Console,
    *,
    label: str,
    defaults: tuple[str, ...],
) -> tuple[str, ...]:
    """Prompt for a replacement state list using numbered defaults."""

    console.print(build_state_table(label, defaults))
    console.print(
        "[dim]Press Enter to keep the defaults. Otherwise enter a comma-separated "
        "mix of numbers and/or names, for example `1,2,Blocked`.[/dim]"
    )
    while True:
        raw_value = Prompt.ask(
            f"{label} selection",
            default="",
            show_default=False,
            console=console,
        ).strip()
        if not raw_value:
            return defaults
        try:
            return parse_state_selection(raw_value, defaults)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")


def parse_state_selection(raw_value: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a comma-separated state selection using numeric default references."""

    selected: list[str] = []
    seen: set[str] = set()
    for token in (part.strip() for part in raw_value.split(",")):
        if not token:
            continue
        if token.isdigit():
            index = int(token)
            if index < 1 or index > len(defaults):
                raise ValueError(f"State number {index} is out of range.")
            state = defaults[index - 1]
        else:
            state = token
        if state not in seen:
            selected.append(state)
            seen.add(state)
    if not selected:
        raise ValueError("Select at least one state.")
    return tuple(selected)


def build_state_table(label: str, defaults: tuple[str, ...]) -> Panel:
    """Build a compact Rich table for numbered state defaults."""

    table = Table(box=box.SIMPLE, expand=False, title=label)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("State", style="white")
    for index, state in enumerate(defaults, start=1):
        table.add_row(str(index), state)
    return Panel(table, border_style="cyan")


def detect_git_repo(target_dir: Path) -> str | None:
    """Best-effort detection of the current directory's origin URL."""

    completed = subprocess.run(
        ["git", "-C", str(target_dir), "config", "--get", "remote.origin.url"],
        check=False,
        capture_output=True,
        text=True,
    )
    detected = completed.stdout.strip()
    return detected or None


def default_project_slug(target_dir: Path) -> str:
    """Return a sensible starter project slug for the current directory."""

    return target_dir.name or "project"


def remove_existing_path(path: Path) -> None:
    """Remove a path if it already exists so bootstrap can replace it."""

    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def target_name(asset_name: str) -> str:
    """Map packaged asset names back to their runtime filenames."""

    if asset_name.endswith(".asset"):
        return asset_name.removesuffix(".asset")
    return asset_name
