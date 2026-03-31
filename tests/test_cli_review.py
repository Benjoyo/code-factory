from __future__ import annotations

from pathlib import Path

import typer
from click.testing import CliRunner

from code_factory.cli import (
    app,
    build_cli_config,
    normalize_cli_args,
)
from code_factory.errors import ReviewError

runner = CliRunner()


def test_normalize_cli_args_preserves_review_command() -> None:
    assert normalize_cli_args(["review", "ENG-1"]) == ["review", "ENG-1"]


def test_cli_help_lists_review() -> None:
    result = runner.invoke(typer.main.get_command(app), ["--help"])
    assert result.exit_code == 0
    assert "review" in result.output


def test_review_command_resolves_workflow_and_keep(tmp_path: Path, monkeypatch) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("prompt\n", encoding="utf-8")
    calls: list[tuple[str, str, bool]] = []

    async def fake_run_review_session(
        workflow_path: str,
        target: str,
        *,
        keep: bool,
        console=None,
    ) -> None:
        calls.append((workflow_path, target, keep))

    monkeypatch.setattr("code_factory.cli.run_review_session", fake_run_review_session)
    result = runner.invoke(
        typer.main.get_command(app),
        ["review", "main", "--workflow", str(workflow), "--keep"],
    )
    assert result.exit_code == 0
    assert calls == [
        (build_cli_config(workflow, None, None).workflow_path, "main", True)
    ]


def test_review_command_surfaces_review_errors(tmp_path: Path, monkeypatch) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("prompt\n", encoding="utf-8")

    async def fake_run_review_session(
        workflow_path: str,
        target: str,
        *,
        keep: bool,
        console=None,
    ) -> None:
        raise ReviewError("review failed")

    monkeypatch.setattr("code_factory.cli.run_review_session", fake_run_review_session)
    result = runner.invoke(
        typer.main.get_command(app),
        ["review", "main", "--workflow", str(workflow)],
    )
    assert result.exit_code == 1
    assert "review failed" in result.output
