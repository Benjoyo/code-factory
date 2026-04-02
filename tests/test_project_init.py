from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console

from code_factory.errors import TrackerClientError
from code_factory.project_init import (
    PreparedProjectInit,
    _format_tracker_error,
    _linear_state_type,
    _required_linear_states,
    _single_team,
    prepare_project_init,
)
from code_factory.trackers.linear.bootstrap import (
    LinearBootstrapProject,
    LinearBootstrapState,
    LinearBootstrapTeam,
)
from code_factory.workflow.template import WorkflowTemplateValues


def sample_values(*, tracker_kind: str = "linear") -> WorkflowTemplateValues:
    return WorkflowTemplateValues(
        tracker_kind=tracker_kind,
        project="demo-project",
        git_repo="git@github.com:example/demo.git",
        failure_state="Human Review",
        active_states=("Todo", "In Progress", "Merging", "Rework"),
        terminal_states=("Canceled", "Duplicate", "Done"),
        workspace_root="/tmp/code-factory-workspaces",
        max_concurrent_agents=2,
    )


class FakeBootstrapper:
    project_result: LinearBootstrapProject | None = None
    team_result = LinearBootstrapTeam(
        id="team-1",
        name="Engineering",
        key="ENG",
        states=(),
    )
    created_project = LinearBootstrapProject(
        id="project-1",
        name="Demo Project",
        slug_id="demo-project-1",
        teams=(team_result,),
    )

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key
        self.created_states: list[tuple[tuple[str, str], ...]] = []
        self.create_project_calls: list[tuple[str, str]] = []

    async def close(self) -> None:
        return None

    async def resolve_project(self, reference: str) -> LinearBootstrapProject | None:
        assert reference == "demo-project"
        return self.project_result

    async def resolve_team(self, reference: str) -> LinearBootstrapTeam:
        assert reference == "ENG"
        return self.team_result

    async def create_project(
        self, *, name: str, team: LinearBootstrapTeam
    ) -> LinearBootstrapProject:
        self.create_project_calls.append((name, team.key))
        return self.created_project

    async def ensure_states(
        self,
        *,
        team: LinearBootstrapTeam,
        required_states: tuple[tuple[str, str], ...],
    ) -> tuple[LinearBootstrapState, ...]:
        self.created_states.append(required_states)
        return tuple(
            LinearBootstrapState(
                id=f"state-{index}",
                name=name,
                type=state_type,
            )
            for index, (name, state_type) in enumerate(required_states, start=1)
        )


class FailingBootstrapper(FakeBootstrapper):
    async def resolve_project(self, reference: str) -> LinearBootstrapProject | None:
        raise TrackerClientError(("tracker_operation_failed", reference))


class EmptyStateBootstrapper(FakeBootstrapper):
    async def ensure_states(
        self,
        *,
        team: LinearBootstrapTeam,
        required_states: tuple[tuple[str, str], ...],
    ) -> tuple[LinearBootstrapState, ...]:
        self.created_states.append(required_states)
        return ()


def test_prepare_project_init_keeps_non_linear_bootstrap_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = sample_values(tracker_kind="memory")
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init", lambda **_: values
    )

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert prepared == PreparedProjectInit(values=values, warnings=())


def test_prepare_project_init_warns_when_linear_auth_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init",
        lambda **_: sample_values(),
    )
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert prepared.values.project == "demo-project"
    assert "tracker.project" in prepared.warnings[0]


def test_prepare_project_init_resolves_existing_project_and_missing_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = LinearBootstrapProject(
        id="project-1",
        name="Demo Project",
        slug_id="canonical-slug",
        teams=(
            LinearBootstrapTeam(
                id="team-1",
                name="Engineering",
                key="ENG",
                states=(
                    LinearBootstrapState(id="1", name="Todo", type="unstarted"),
                    LinearBootstrapState(id="2", name="In Progress", type="started"),
                ),
            ),
        ),
    )
    fake = FakeBootstrapper(api_key="token")
    fake.project_result = project
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init", lambda **_: sample_values()
    )
    monkeypatch.setattr(
        "code_factory.project_init.build_tracker_bootstrapper",
        lambda **_: fake,
    )
    monkeypatch.setattr(
        "code_factory.project_init.Confirm.ask", lambda *args, **kwargs: True
    )
    monkeypatch.setenv("LINEAR_API_KEY", "token")

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert prepared.values.project == "demo-project"
    assert prepared.warnings == ()
    assert fake.created_states == [
        (
            ("Merging", "started"),
            ("Rework", "started"),
            ("Human Review", "started"),
            ("Canceled", "canceled"),
            ("Duplicate", "duplicate"),
            ("Done", "completed"),
        )
    ]


def test_prepare_project_init_creates_missing_project_and_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBootstrapper(api_key="token")
    answers = iter([True])
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init", lambda **_: sample_values()
    )
    monkeypatch.setattr(
        "code_factory.project_init.build_tracker_bootstrapper",
        lambda **_: fake,
    )
    monkeypatch.setattr(
        "code_factory.project_init.Confirm.ask",
        lambda *args, **kwargs: next(answers),
    )
    monkeypatch.setattr(
        "code_factory.project_init.prompt_non_empty",
        lambda label, **kwargs: "ENG",
    )
    monkeypatch.setenv("LINEAR_API_KEY", "token")

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert prepared.values.project == "demo-project"
    assert fake.create_project_calls == [("demo-project", "ENG")]
    assert fake.created_states[0][0] == ("Todo", "unstarted")


def test_prepare_project_init_warns_for_multi_team_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBootstrapper(api_key="token")
    fake.project_result = LinearBootstrapProject(
        id="project-1",
        name="Demo Project",
        slug_id="canonical-slug",
        teams=(
            replace(fake.team_result, id="team-1"),
            replace(fake.team_result, id="team-2", key="WEB"),
        ),
    )
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init", lambda **_: sample_values()
    )
    monkeypatch.setattr(
        "code_factory.project_init.build_tracker_bootstrapper",
        lambda **_: fake,
    )
    monkeypatch.setenv("LINEAR_API_KEY", "token")

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert prepared.values.project == "demo-project"
    assert "multiple teams" in prepared.warnings[0]


def test_prepare_project_init_warns_when_project_creation_is_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBootstrapper(api_key="token")
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init", lambda **_: sample_values()
    )
    monkeypatch.setattr(
        "code_factory.project_init.build_tracker_bootstrapper",
        lambda **_: fake,
    )
    monkeypatch.setattr(
        "code_factory.project_init.Confirm.ask", lambda *args, **kwargs: False
    )
    monkeypatch.setenv("LINEAR_API_KEY", "token")

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert "was not created" in prepared.warnings[0]


def test_prepare_project_init_warns_when_linear_bootstrapper_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init", lambda **_: sample_values()
    )
    monkeypatch.setattr(
        "code_factory.project_init.build_tracker_bootstrapper",
        lambda **_: FailingBootstrapper(api_key="token"),
    )
    monkeypatch.setenv("LINEAR_API_KEY", "token")

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert "Skipping Linear project verification/provisioning" in prepared.warnings[0]


def test_prepare_project_init_handles_no_missing_states_and_declined_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBootstrapper(api_key="token")
    fake.project_result = LinearBootstrapProject(
        id="project-1",
        name="Demo Project",
        slug_id="canonical-slug",
        teams=(
            LinearBootstrapTeam(
                id="team-1",
                name="Engineering",
                key="ENG",
                states=tuple(
                    LinearBootstrapState(id=str(index), name=name, type="started")
                    for index, name in enumerate(
                        (
                            "Todo",
                            "In Progress",
                            "Merging",
                            "Rework",
                            "Human Review",
                            "Canceled",
                            "Duplicate",
                            "Done",
                        ),
                        start=1,
                    )
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init", lambda **_: sample_values()
    )
    monkeypatch.setattr(
        "code_factory.project_init.build_tracker_bootstrapper",
        lambda **_: fake,
    )
    monkeypatch.setenv("LINEAR_API_KEY", "token")

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert prepared.values.project == "demo-project"
    assert fake.created_states == []

    fake.project_result = LinearBootstrapProject(
        id="project-2",
        name="Demo Project",
        slug_id="canonical-slug",
        teams=(
            LinearBootstrapTeam(
                id="team-1",
                name="Engineering",
                key="ENG",
                states=(LinearBootstrapState(id="1", name="Todo", type="unstarted"),),
            ),
        ),
    )
    monkeypatch.setattr(
        "code_factory.project_init.Confirm.ask", lambda *args, **kwargs: False
    )

    prepared = prepare_project_init(console=Console(record=True), target_dir=Path.cwd())

    assert "not provisioned" in prepared.warnings[0]


def test_project_init_helper_functions_cover_edge_cases() -> None:
    with pytest.raises(TrackerClientError, match="tracker_missing_field"):
        _single_team(
            LinearBootstrapProject(
                id="project-1",
                name="Demo",
                slug_id="demo",
                teams=(),
            )
        )

    deduped = _required_linear_states(
        replace(
            sample_values(),
            active_states=("Todo", "Todo", "In Progress"),
            terminal_states=("Done", "Done"),
        )
    )
    assert deduped == (
        ("Todo", "unstarted"),
        ("In Progress", "started"),
        ("Human Review", "started"),
        ("Done", "completed"),
    )
    assert _linear_state_type("Custom", sample_values()) == "started"
    assert _format_tracker_error(TrackerClientError("boom")) == "boom"
    assert _format_tracker_error(TrackerClientError(("only-code",))) == "('only-code',)"


def test_prepare_project_init_skips_console_messages_when_no_states_are_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EmptyStateBootstrapper(api_key="token")
    monkeypatch.setattr(
        "code_factory.project_init.prompt_project_init", lambda **_: sample_values()
    )
    monkeypatch.setattr(
        "code_factory.project_init.build_tracker_bootstrapper",
        lambda **_: fake,
    )
    monkeypatch.setattr(
        "code_factory.project_init.Confirm.ask", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        "code_factory.project_init.prompt_non_empty",
        lambda label, **kwargs: "ENG",
    )
    monkeypatch.setenv("LINEAR_API_KEY", "token")
    console = Console(record=True)

    prepared = prepare_project_init(console=console, target_dir=Path.cwd())

    assert prepared.warnings == ()
    assert "Provisioned Linear project" not in console.export_text()

    fake.project_result = LinearBootstrapProject(
        id="project-1",
        name="Demo Project",
        slug_id="canonical-slug",
        teams=(
            LinearBootstrapTeam(
                id="team-1",
                name="Engineering",
                key="ENG",
                states=(LinearBootstrapState(id="1", name="Todo", type="unstarted"),),
            ),
        ),
    )
    second_console = Console(record=True)
    prepared = prepare_project_init(console=second_console, target_dir=Path.cwd())

    assert prepared.warnings == ()
    assert "Created " not in second_console.export_text()
