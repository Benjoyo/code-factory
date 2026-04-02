from __future__ import annotations

"""Bootstrap orchestration for `cf init`, including optional Linear setup."""

import asyncio
import os
from dataclasses import dataclass, replace
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

from .application.bootstrap import prompt_non_empty, prompt_project_init
from .errors import TrackerClientError
from .trackers.bootstrap import (
    LinearBootstrapProject,
    LinearBootstrapTeam,
    ProjectBootstrapper,
    build_tracker_bootstrapper,
)
from .workflow.template import WorkflowTemplateValues


@dataclass(frozen=True, slots=True)
class PreparedProjectInit:
    values: WorkflowTemplateValues
    warnings: tuple[str, ...]


def prepare_project_init(
    *,
    console: Console | None = None,
    target_dir: Path | None = None,
) -> PreparedProjectInit:
    resolved_console = console or Console()
    values = prompt_project_init(console=resolved_console, target_dir=target_dir)
    if values.tracker_kind != "linear":
        return PreparedProjectInit(values=values, warnings=())

    api_key = os.getenv("LINEAR_API_KEY")
    if not api_key:
        return PreparedProjectInit(values=values, warnings=(_manual_project_warning(),))
    try:
        prepared = asyncio.run(
            _prepare_linear_project_init(
                values=values,
                console=resolved_console,
                bootstrapper=build_tracker_bootstrapper(
                    tracker_kind="linear",
                    api_key=api_key,
                ),
            )
        )
    except TrackerClientError as exc:
        return PreparedProjectInit(
            values=values,
            warnings=(
                "Skipping Linear project verification/provisioning: "
                f"{_format_tracker_error(exc)}. {_manual_project_warning()}",
            ),
        )
    return prepared


async def _prepare_linear_project_init(
    *,
    values: WorkflowTemplateValues,
    console: Console,
    bootstrapper: ProjectBootstrapper,
) -> PreparedProjectInit:
    try:
        project = await bootstrapper.resolve_project(values.project)
        if project is None:
            return await _create_or_skip_project(
                values=values,
                console=console,
                bootstrapper=bootstrapper,
            )
        return await _reconcile_existing_project(
            values=values,
            project=project,
            console=console,
            bootstrapper=bootstrapper,
        )
    finally:
        await bootstrapper.close()


async def _create_or_skip_project(
    *,
    values: WorkflowTemplateValues,
    console: Console,
    bootstrapper: ProjectBootstrapper,
) -> PreparedProjectInit:
    if not Confirm.ask(
        "Linear project not found. Create it automatically?",
        default=True,
        console=console,
    ):
        return PreparedProjectInit(
            values=values,
            warnings=(
                "Linear project was not created; create it manually or update "
                "`tracker.project` before running the service.",
            ),
        )
    team_name = prompt_non_empty("Linear team (name or key)", console=console)
    team = await bootstrapper.resolve_team(team_name)
    project = await bootstrapper.create_project(name=values.project, team=team)
    created = await bootstrapper.ensure_states(
        team=_single_team(project),
        required_states=_missing_state_specs(
            _required_linear_states(values), _single_team(project)
        ),
    )
    if created:
        console.print(
            f"Provisioned Linear project `{project.name}` and created "
            f"{len(created)} workflow state(s) on team `{_single_team(project).key}`."
        )
    return PreparedProjectInit(values=values, warnings=())


async def _reconcile_existing_project(
    *,
    values: WorkflowTemplateValues,
    project: LinearBootstrapProject,
    console: Console,
    bootstrapper: ProjectBootstrapper,
) -> PreparedProjectInit:
    if len(project.teams) != 1:
        return PreparedProjectInit(
            values=values,
            warnings=(
                "Resolved Linear project has multiple teams; skipping workflow "
                "state provisioning because `cf init` only supports single-team projects.",
            ),
        )
    team = project.teams[0]
    missing = _missing_state_specs(_required_linear_states(values), team)
    if not missing:
        return PreparedProjectInit(values=values, warnings=())
    if not Confirm.ask(
        f"Create missing workflow states on team `{team.key}` automatically?",
        default=True,
        console=console,
    ):
        return PreparedProjectInit(
            values=values,
            warnings=(
                "Selected workflow states were not provisioned in Linear; "
                "create them manually before running the service.",
            ),
        )
    created = await bootstrapper.ensure_states(team=team, required_states=missing)
    if created:
        console.print(
            f"Created {len(created)} missing Linear workflow state(s) on team `{team.key}`."
        )
    return PreparedProjectInit(values=values, warnings=())


def _single_team(project: LinearBootstrapProject) -> LinearBootstrapTeam:
    if len(project.teams) != 1:
        raise TrackerClientError(("tracker_missing_field", "`team` is required"))
    return project.teams[0]


def _required_linear_states(
    values: WorkflowTemplateValues,
) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for name in (*values.active_states, values.failure_state, *values.terminal_states):
        lowered = name.strip().lower()
        if lowered in seen:
            continue
        ordered.append((name, _linear_state_type(name, values)))
        seen.add(lowered)
    return tuple(ordered)


def _missing_state_specs(
    required_states: tuple[tuple[str, str], ...],
    team: LinearBootstrapTeam,
) -> tuple[tuple[str, str], ...]:
    existing = {state.name.strip().lower() for state in team.states}
    return tuple(
        state for state in required_states if state[0].strip().lower() not in existing
    )


def _linear_state_type(name: str, values: WorkflowTemplateValues) -> str:
    lowered = name.strip().lower()
    if lowered == "todo":
        return "unstarted"
    if lowered in {"done"}:
        return "completed"
    if lowered in {"canceled", "cancelled"}:
        return "canceled"
    if lowered == "duplicate":
        return "duplicate"
    if lowered in {
        "in progress",
        "rework",
        "merging",
        values.failure_state.strip().lower(),
    }:
        return "started"
    return "started"


def _manual_project_warning() -> str:
    return (
        "Linear verification was skipped, so `WORKFLOW.md` will use the entered "
        "project value as `tracker.project`."
    )


def _format_tracker_error(error: TrackerClientError) -> str:
    reason = error.reason
    if isinstance(reason, tuple) and len(reason) > 1:
        return str(reason[1])
    return str(reason)
