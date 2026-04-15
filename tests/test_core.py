from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from code_factory.application import CodeFactoryService
from code_factory.config import parse_settings
from code_factory.errors import ConfigValidationError, WorkflowLoadError, WorkspaceError
from code_factory.prompts import build_prompt
from code_factory.workflow import load_workflow, parse_workflow
from code_factory.workspace import WorkspaceManager

from .conftest import make_issue, make_snapshot, write_workflow_file


def test_parse_workflow_accepts_prompt_only_files() -> None:
    loaded = parse_workflow("Prompt only\n")
    assert loaded.config == {}
    assert loaded.prompt_template == "Prompt only"


def test_workflow_load_rejects_non_map_front_matter(tmp_path: Path) -> None:
    workflow = tmp_path / "INVALID_WORKFLOW.md"
    workflow.write_text("---\n- not-a-map\n---\nPrompt body\n", encoding="utf-8")

    with pytest.raises(WorkflowLoadError):
        load_workflow(str(workflow))


def test_workflow_load_requires_states_mapping(tmp_path: Path) -> None:
    workflow = tmp_path / "PROMPT_ONLY_WORKFLOW.md"
    workflow.write_text("Prompt only\n", encoding="utf-8")

    with pytest.raises(ConfigValidationError, match="states is required"):
        load_workflow(str(workflow))


def test_config_defaults_and_env_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "env-linear-token")
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        tracker={"api_key": None, "project": "project"},
        agent={"max_retry_backoff_ms": 5_000},
    )
    settings = parse_settings(load_workflow(str(workflow)).config)
    assert settings.tracker.api_key == "env-linear-token"
    assert settings.polling.interval_ms == 30_000
    assert settings.tracker.active_states == ("Todo", "In Progress")
    assert settings.terminal_states == (
        "Closed",
        "Cancelled",
        "Canceled",
        "Duplicate",
        "Done",
    )
    assert settings.agent.max_retry_backoff_ms == 5_000
    assert settings.agent.max_worker_retries == 3
    assert settings.failure_state == "Human Review"
    assert settings.observability.file_logging.enabled is True
    assert settings.observability.file_logging.root is None

    workflow = write_workflow_file(
        tmp_path / "BAD_WORKFLOW.md", polling={"interval_ms": "invalid"}
    )
    with pytest.raises(ConfigValidationError):
        parse_settings(load_workflow(str(workflow)).config)

    workflow = write_workflow_file(
        tmp_path / "DEPRECATED_AGENT_KEY.md", agent={"max_turns": 5}
    )
    with pytest.raises(ConfigValidationError, match="agent has unsupported keys"):
        parse_settings(load_workflow(str(workflow)).config)

    workflow = write_workflow_file(
        tmp_path / "BAD_FILE_LOGGING_KEY.md",
        observability={"file_logging": {"mode": "always"}},
    )
    with pytest.raises(
        ConfigValidationError,
        match="observability.file_logging has unsupported keys: mode",
    ):
        parse_settings(load_workflow(str(workflow)).config)

    workflow = tmp_path / "MISSING_FAILURE_STATE.md"
    workflow.write_text(
        "---\ntracker:\n  kind: linear\n  project: project\nstates:\n  Todo:\n    prompt: default\n---\n# prompt: default\nBody\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError, match="failure_state is required"):
        parse_settings(load_workflow(str(workflow)).config)


def test_multi_state_workflow_parses_sections_and_derives_active_states(
    tmp_path: Path,
) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        prompt=(
            "# prompt: default\n"
            "Shared instructions for {{ issue.state }}.\n\n"
            "# prompt: merge\n"
            "Merge-only instructions.\n"
        ),
        codex={"model": "gpt-5.4", "reasoning_effort": "high", "fast_mode": True},
        states={
            "Todo": {"prompt": "default"},
            "In Progress": {"prompt": "default"},
            "Merging": {
                "prompt": ["default", "merge"],
                "codex": {
                    "model": "gpt-5.4-mini",
                    "reasoning_effort": "low",
                    "fast_mode": False,
                    "skills": ["land"],
                },
            },
        },
    )

    loaded = load_workflow(str(workflow))
    settings = parse_settings(loaded.config)
    snapshot = make_snapshot(workflow)

    assert loaded.prompt_template == ""
    assert loaded.prompt_sections == {
        "default": "Shared instructions for {{ issue.state }}.",
        "merge": "Merge-only instructions.",
    }
    assert snapshot.prompt_template == ""
    assert settings.tracker.active_states == ("Todo", "In Progress", "Merging")
    assert (
        snapshot.settings_for_state("Todo").coding_agent.model
        == snapshot.settings.coding_agent.model
    )
    assert snapshot.settings_for_state("Todo").coding_agent.fast_mode is True
    assert snapshot.settings_for_state("Todo").coding_agent.repo_skill_allowlist is None
    assert snapshot.settings_for_state("Merging").coding_agent.model == "gpt-5.4-mini"
    assert snapshot.settings_for_state("Merging").coding_agent.reasoning_effort == "low"
    assert snapshot.settings_for_state("Merging").coding_agent.fast_mode is False
    assert snapshot.settings_for_state("Merging").coding_agent.repo_skill_allowlist == (
        "land",
    )
    merging_profile = snapshot.state_profile("Merging")
    assert merging_profile is not None
    assert merging_profile.merge.mode == "agent_only"
    prompt = build_prompt(make_issue(state="Merging"), snapshot)
    assert "Shared instructions for Merging." in prompt
    assert "Merge-only instructions." in prompt


def test_multi_state_workflow_rejects_stray_body_content(tmp_path: Path) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        "---\ntracker:\n  kind: linear\nstates:\n  Todo:\n    prompt: default\n---\n"
        "stray content\n\n# prompt: default\nShared prompt.\n",
        encoding="utf-8",
    )

    with pytest.raises(
        WorkflowLoadError, match="workflow_prompt_section_stray_content"
    ):
        load_workflow(str(workflow))


def test_multi_state_workflow_rejects_missing_prompt_ref(tmp_path: Path) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        prompt="# prompt: default\nShared prompt.\n",
        states={"Todo": {"prompt": "missing"}},
    )

    with pytest.raises(
        ConfigValidationError, match="references missing prompt section 'missing'"
    ):
        make_snapshot(workflow)


def test_settings_require_states_mapping(tmp_path: Path) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        "---\ntracker:\n  kind: linear\n  project: project\n---\n# prompt: default\nBody\n",
        encoding="utf-8",
    )
    definition = parse_workflow(workflow.read_text(encoding="utf-8"))

    with pytest.raises(ConfigValidationError, match="states is required"):
        parse_settings(definition.config)


def test_snapshot_state_profile_helpers_fall_back_without_profile(
    tmp_path: Path,
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", prompt="Shared prompt.")
    snapshot = make_snapshot(workflow)

    assert snapshot.prompt_template_for_state("Review") == snapshot.prompt_template
    assert snapshot.settings_for_state("Review") == snapshot.settings


@pytest.mark.asyncio
async def test_workspace_is_deterministic_and_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md", workspace={"root": str(workspace_root)}
    )
    snapshot = make_snapshot(workflow)
    manager = WorkspaceManager(snapshot.settings)

    first = await manager.create_for_issue("MT/Det")
    second = await manager.create_for_issue("MT/Det")
    assert first.path == second.path
    assert Path(first.path).name == "MT_Det"

    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_target = workspace_root / "MT-SYM"
    workspace_root.mkdir(exist_ok=True)
    symlink_target.symlink_to(outside)

    with pytest.raises(WorkspaceError):
        await manager.create_for_issue("MT-SYM")


def test_prompt_builder_is_strict_and_uses_default_template(tmp_path: Path) -> None:
    invalid = write_workflow_file(
        tmp_path / "STRICT.md", prompt="Work on {{ missing.ticket_id }}"
    )
    snapshot = make_snapshot(invalid)
    issue = make_issue()
    with pytest.raises(RuntimeError, match="template_render_error:"):
        build_prompt(issue, snapshot)

    blank = write_workflow_file(tmp_path / "BLANK.md", prompt="   \n")
    snapshot = make_snapshot(blank)
    issue = make_issue(description=None)
    issue_data = asdict(issue) | {
        "upstream_tickets": [
            {
                "id": "upstream-1",
                "identifier": "ENG-UP-1",
                "title": "Build pipeline",
                "state": "Done",
                "results_by_state": {
                    "Build": {
                        "decision": "transition",
                        "next_state": "Done",
                        "summary": "artifact ready",
                    }
                },
            }
        ]
    }
    prompt = build_prompt(issue, snapshot, issue_data=issue_data)
    assert "You are working on a tracked issue." in prompt
    assert "ENG-UP-1: Build pipeline [id: upstream-1] (Done)" in prompt
    assert "Build summary: artifact ready" in prompt
    assert "No description provided." in prompt


def test_default_prompt_renders_ticket_specific_review_command(tmp_path: Path) -> None:
    template = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "code_factory"
        / "workflow"
        / "templates"
        / "default.md"
    ).read_text(encoding="utf-8")
    prompt = template.split("---\n", 2)[2].strip()
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        prompt=prompt,
        states={
            "Todo": {"auto_next_state": "In Progress"},
            "In Progress": {"prompt": ["base", "execute"]},
        },
    )
    snapshot = make_snapshot(workflow)
    issue = make_issue(identifier="ENG-12")
    issue_data = asdict(issue) | {"upstream_tickets": []}

    rendered = build_prompt(issue, snapshot, issue_data=issue_data)

    assert "cf review ENG-12" in rendered


@pytest.mark.asyncio
async def test_service_fails_startup_preflight_for_invalid_dispatch_config(
    tmp_path: Path,
) -> None:
    workflow = write_workflow_file(
        tmp_path / "INVALID_WORKFLOW.md",
        tracker={"kind": None, "api_key": None, "project": None},
    )

    with pytest.raises(ConfigValidationError, match="tracker.kind is required"):
        await CodeFactoryService(str(workflow)).run_forever()
