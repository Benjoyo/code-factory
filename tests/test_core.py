from __future__ import annotations

from pathlib import Path

import pytest

from code_factory.application import CodeFactoryService
from code_factory.config import parse_settings
from code_factory.errors import ConfigValidationError, WorkflowLoadError, WorkspaceError
from code_factory.prompts import build_prompt
from code_factory.workflow import load_workflow
from code_factory.workspace import WorkspaceManager

from .conftest import make_issue, make_snapshot, write_workflow_file


def test_workflow_load_accepts_prompt_only_files(tmp_path: Path) -> None:
    workflow = tmp_path / "PROMPT_ONLY_WORKFLOW.md"
    workflow.write_text("Prompt only\n", encoding="utf-8")

    loaded = load_workflow(str(workflow))
    assert loaded.config == {}
    assert loaded.prompt_template == "Prompt only"


def test_workflow_load_rejects_non_map_front_matter(tmp_path: Path) -> None:
    workflow = tmp_path / "INVALID_WORKFLOW.md"
    workflow.write_text("---\n- not-a-map\n---\nPrompt body\n", encoding="utf-8")

    with pytest.raises(WorkflowLoadError):
        load_workflow(str(workflow))


def test_config_defaults_and_env_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "env-linear-token")
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        tracker={"api_key": None, "project_slug": "project"},
        agent={"max_turns": 5},
    )
    settings = parse_settings(load_workflow(str(workflow)).config)
    assert settings.tracker.api_key == "env-linear-token"
    assert settings.polling.interval_ms == 30_000
    assert settings.tracker.active_states == ("Todo", "In Progress")
    assert settings.agent.max_turns == 5

    workflow = write_workflow_file(
        tmp_path / "BAD_WORKFLOW.md", polling={"interval_ms": "invalid"}
    )
    with pytest.raises(ConfigValidationError):
        parse_settings(load_workflow(str(workflow)).config)


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
    prompt = build_prompt(make_issue(description=None), snapshot)
    assert "You are working on a tracked issue." in prompt
    assert "No description provided." in prompt


@pytest.mark.asyncio
async def test_service_fails_startup_preflight_for_invalid_dispatch_config(
    tmp_path: Path,
) -> None:
    workflow = write_workflow_file(
        tmp_path / "INVALID_WORKFLOW.md",
        tracker={"kind": None, "api_key": None, "project_slug": None},
    )

    with pytest.raises(ConfigValidationError, match="tracker.kind is required"):
        await CodeFactoryService(str(workflow)).run_forever()
