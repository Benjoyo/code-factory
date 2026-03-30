from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console
from rich.table import Table

from code_factory.application.bootstrap import (
    DEFAULT_ACTIVE_STATES,
    DEFAULT_FAILURE_STATE,
    DEFAULT_TERMINAL_STATES,
    ProjectInitResult,
    build_state_table,
    copy_bootstrap_skills,
    default_project_slug,
    detect_git_repo,
    initialize_project,
    parse_state_selection,
    prompt_non_empty,
    prompt_positive_int,
    prompt_project_init,
    prompt_state_list,
    remove_existing_path,
)
from code_factory.workflow.loader import DEFAULT_WORKFLOW_FILENAME
from code_factory.workflow.template import (
    WorkflowTemplateValues,
    default_workflow_template,
    initialize_workflow,
    render_default_workflow,
    token,
    yaml_list,
    yaml_string,
)


def sample_values() -> WorkflowTemplateValues:
    return WorkflowTemplateValues(
        tracker_kind="linear",
        project_slug="demo-project",
        git_repo="git@github.com:example/demo.git",
        failure_state="Human Review",
        active_states=("Todo", "In Progress"),
        terminal_states=("Done", "Canceled"),
        workspace_root="/tmp/code-factory-workspaces",
        max_concurrent_agents=2,
    )


def test_render_default_workflow_replaces_template_tokens() -> None:
    rendered = render_default_workflow(sample_values())

    assert token("TRACKER_KIND") not in rendered
    assert '"linear"' in rendered
    assert '"demo-project"' in rendered
    assert "git clone --depth 1 git@github.com:example/demo.git ." in rendered
    assert "make setup" not in rendered
    assert 'failure_state: "Human Review"' in rendered
    assert '  "Todo":\n    auto_next_state: In Progress' in rendered
    assert (
        '  "In Progress":\n'
        "    prompt: default\n"
        "    completion:\n"
        "      require_pushed_head: true\n"
        "      require_pr: true"
    ) in rendered
    assert "  max_concurrent_agents: 2" in rendered
    assert "# prompt: default" in rendered
    assert "{{ issue.identifier }}" in rendered
    assert "Blocked-by tickets:" in rendered
    assert "merge and delete the head branch" in rendered
    assert (
        "Treat explicit user steering during the run as authoritative task input."
        in rendered
    )
    assert (
        "Never remove already-implemented behavior solely because the original ticket text is stale"
        in rendered
    )


def test_default_workflow_template_contains_meta_tokens() -> None:
    template = default_workflow_template()

    assert token("PROJECT_SLUG") in template
    assert token("FAILURE_STATE") in template
    assert token("STATE_PROFILES") in template


def test_initialize_workflow_writes_rendered_template(tmp_path: Path) -> None:
    written = initialize_workflow(
        tmp_path / DEFAULT_WORKFLOW_FILENAME,
        values=sample_values(),
    )

    assert written.read_text(encoding="utf-8") == render_default_workflow(
        sample_values()
    )


def test_initialize_workflow_rejects_existing_file(tmp_path: Path) -> None:
    workflow = tmp_path / DEFAULT_WORKFLOW_FILENAME
    workflow.write_text("existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        initialize_workflow(workflow, values=sample_values())


def test_yaml_helpers_render_yaml_safe_fragments() -> None:
    assert yaml_string("In Progress") == '"In Progress"'
    assert yaml_list(("Todo", "In Progress")) == '    - "Todo"\n    - "In Progress"'


def test_parse_state_selection_accepts_numbers_names_and_dedupes() -> None:
    assert parse_state_selection("1,2,Blocked,2", DEFAULT_ACTIVE_STATES) == (
        "Todo",
        "In Progress",
        "Blocked",
    )


@pytest.mark.parametrize("raw_value", ["", " , ", "9"])
def test_parse_state_selection_rejects_invalid_values(raw_value: str) -> None:
    with pytest.raises(ValueError):
        parse_state_selection(raw_value, DEFAULT_ACTIVE_STATES)


def test_prompt_state_list_keeps_defaults_on_empty_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "code_factory.application.bootstrap.Prompt.ask",
        lambda *args, **kwargs: "",
    )

    assert (
        prompt_state_list(
            Console(record=True), label="Active states", defaults=DEFAULT_ACTIVE_STATES
        )
        == DEFAULT_ACTIVE_STATES
    )


def test_prompt_state_list_reprompts_after_invalid_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["9", "1,Custom"])
    monkeypatch.setattr(
        "code_factory.application.bootstrap.Prompt.ask",
        lambda *args, **kwargs: next(answers),
    )
    console = Console(record=True)

    assert prompt_state_list(
        console, label="Terminal states", defaults=DEFAULT_TERMINAL_STATES
    ) == ("Canceled", "Custom")
    assert "out of range" in console.export_text()


def test_prompt_non_empty_reprompts_until_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["", "demo"])
    monkeypatch.setattr(
        "code_factory.application.bootstrap.Prompt.ask",
        lambda *args, **kwargs: next(answers),
    )
    console = Console(record=True)

    assert prompt_non_empty("Project slug", console=console, default=None) == "demo"
    assert "cannot be empty" in console.export_text()


def test_prompt_positive_int_reprompts_until_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([0, 2])
    monkeypatch.setattr(
        "code_factory.application.bootstrap.IntPrompt.ask",
        lambda *args, **kwargs: next(answers),
    )
    console = Console(record=True)

    assert prompt_positive_int("Max concurrent agents", console=console, default=2) == 2
    assert "must be greater than zero" in console.export_text()


def test_build_state_table_wraps_table_in_panel() -> None:
    panel = build_state_table("Active states", DEFAULT_ACTIVE_STATES)
    table = cast(Table, panel.renderable)

    assert table.title == "Active states"


def test_detect_git_repo_returns_trimmed_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "code_factory.application.bootstrap.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="git@github.com:example/demo.git\n"
        ),
    )

    assert detect_git_repo(Path.cwd()) == "git@github.com:example/demo.git"


def test_detect_git_repo_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "code_factory.application.bootstrap.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout=""
        ),
    )

    assert detect_git_repo(Path.cwd()) is None


def test_default_project_slug_uses_directory_name(tmp_path: Path) -> None:
    assert default_project_slug(tmp_path / "demo-project") == "demo-project"


def test_prompt_project_init_collects_all_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["linear"])
    monkeypatch.setattr(
        "code_factory.application.bootstrap.Prompt.ask",
        lambda *args, **kwargs: next(answers),
    )
    monkeypatch.setattr(
        "code_factory.application.bootstrap.prompt_non_empty",
        lambda label, **kwargs: {
            "Project slug": "demo-project",
            "Git repository": "git@github.com:example/demo.git",
            "Failure state": DEFAULT_FAILURE_STATE,
            "Workspace root": "/tmp/code-factory-workspaces",
        }[label],
    )
    monkeypatch.setattr(
        "code_factory.application.bootstrap.prompt_state_list",
        lambda console, **kwargs: (
            DEFAULT_ACTIVE_STATES
            if kwargs["label"] == "Active states"
            else DEFAULT_TERMINAL_STATES
        ),
    )
    monkeypatch.setattr(
        "code_factory.application.bootstrap.prompt_positive_int",
        lambda *args, **kwargs: 4,
    )

    assert prompt_project_init(console=Console(record=True), target_dir=Path.cwd()) == (
        WorkflowTemplateValues(
            tracker_kind="linear",
            project_slug="demo-project",
            git_repo="git@github.com:example/demo.git",
            failure_state=DEFAULT_FAILURE_STATE,
            active_states=DEFAULT_ACTIVE_STATES,
            terminal_states=DEFAULT_TERMINAL_STATES,
            workspace_root="/tmp/code-factory-workspaces",
            max_concurrent_agents=4,
        )
    )


def test_initialize_project_writes_workflow_and_skills(tmp_path: Path) -> None:
    result = initialize_project(sample_values(), target_dir=tmp_path)

    assert isinstance(result, ProjectInitResult)
    assert result.workflow_path.is_file()
    assert result.skills_path.is_dir()
    assert (result.skills_path / "commit" / "SKILL.md").is_file()
    assert (result.skills_path / "land" / "land_watch.py").is_file()


def test_initialize_project_rejects_existing_paths(tmp_path: Path) -> None:
    workflow = tmp_path / DEFAULT_WORKFLOW_FILENAME
    workflow.write_text("existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        initialize_project(sample_values(), target_dir=tmp_path)


def test_initialize_project_rejects_existing_skills_without_force(
    tmp_path: Path,
) -> None:
    skills_path = tmp_path / ".agents" / "skills"
    skills_path.mkdir(parents=True)

    with pytest.raises(FileExistsError):
        initialize_project(sample_values(), target_dir=tmp_path)


def test_initialize_project_force_replaces_existing_skills(tmp_path: Path) -> None:
    skills_path = tmp_path / ".agents" / "skills"
    skills_path.mkdir(parents=True)
    (skills_path / "stale.txt").write_text("stale\n", encoding="utf-8")

    result = initialize_project(sample_values(), target_dir=tmp_path, force=True)

    assert not (skills_path / "stale.txt").exists()
    assert (result.skills_path / "push" / "SKILL.md").is_file()


def test_copy_bootstrap_skills_copies_packaged_tree(tmp_path: Path) -> None:
    destination = tmp_path / ".agents" / "skills"

    copy_bootstrap_skills(destination)

    assert (destination / "debug" / "SKILL.md").is_file()
    assert "--delete-branch" in (destination / "land" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Avoid discretionary code changes during merge" in (
        destination / "land" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_remove_existing_path_handles_file_and_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    dir_path = tmp_path / "dir"
    file_path.write_text("content\n", encoding="utf-8")
    dir_path.mkdir()

    remove_existing_path(file_path)
    remove_existing_path(dir_path)
    remove_existing_path(tmp_path / "missing")

    assert not file_path.exists()
    assert not dir_path.exists()
