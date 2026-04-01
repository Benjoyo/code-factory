from __future__ import annotations

from pathlib import Path

import typer
from click import Context, Group
from click.testing import CliRunner

from code_factory.cli import app, normalize_cli_args, resolve_control_endpoint
from code_factory.config.defaults import DEFAULT_SERVER_PORT
from code_factory.errors import ControlRequestError
from code_factory.observability.api.client import ControlEndpoint
from code_factory.observability.runtime_metadata import (
    clear_runtime_metadata,
    runtime_metadata_path,
    write_runtime_metadata,
)
from code_factory.trackers.linear.ops.ops_files import read_text_file

from ..conftest import write_workflow_file

runner = CliRunner()


def test_normalize_cli_args_preserves_steer_command() -> None:
    assert normalize_cli_args(["steer", "ENG-1", "focus"]) == [
        "steer",
        "ENG-1",
        "focus",
    ]
    assert normalize_cli_args(["issue", "get", "ENG-1"]) == [
        "issue",
        "get",
        "ENG-1",
    ]
    assert normalize_cli_args(["comment", "list", "ENG-1"]) == [
        "comment",
        "list",
        "ENG-1",
    ]
    assert normalize_cli_args(["workpad", "sync", "ENG-1"]) == [
        "workpad",
        "sync",
        "ENG-1",
    ]
    assert normalize_cli_args(
        ["tracker", "raw", "--query", "query Viewer { viewer { id } }"]
    ) == ["tracker", "raw", "--query", "query Viewer { viewer { id } }"]


def test_cli_registers_tracker_groups() -> None:
    command = typer.main.get_command(app)
    assert isinstance(command, Group)
    context = Context(command)
    assert {"issue", "comment", "workpad", "tracker"}.issubset(
        set(command.list_commands(context))
    )


def test_resolve_control_endpoint_prefers_runtime_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    workflow = tmp_path / "WORKFLOW.md"
    write_runtime_metadata(str(workflow.resolve()), host="127.0.0.1", port=4321, pid=99)
    try:
        endpoint, resolved_workflow = resolve_control_endpoint(None, None)
    finally:
        clear_runtime_metadata(str(workflow.resolve()))
    assert endpoint == ControlEndpoint("127.0.0.1", 4321)
    assert resolved_workflow == str(workflow.resolve())


def test_resolve_control_endpoint_uses_default_or_cli_override(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    default_endpoint, _ = resolve_control_endpoint(None, None)
    override_endpoint, _ = resolve_control_endpoint(None, 8765)
    assert default_endpoint == ControlEndpoint("127.0.0.1", DEFAULT_SERVER_PORT)
    assert override_endpoint == ControlEndpoint("127.0.0.1", 8765)


def test_resolve_control_endpoint_ignores_invalid_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    workflow = tmp_path / "WORKFLOW.md"
    path = runtime_metadata_path(str(workflow.resolve()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    endpoint, _ = resolve_control_endpoint(None, None)
    assert endpoint == ControlEndpoint("127.0.0.1", DEFAULT_SERVER_PORT)
    path.write_text('{"host": null, "port": "bad"}', encoding="utf-8")
    endpoint, _ = resolve_control_endpoint(None, None)
    assert endpoint == ControlEndpoint("127.0.0.1", DEFAULT_SERVER_PORT)


def test_steer_command_uses_discovery_and_prints_acceptance(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("prompt\n", encoding="utf-8")
    write_runtime_metadata(str(workflow.resolve()), host="127.0.0.1", port=4555, pid=12)
    calls: list[tuple[ControlEndpoint, str, str]] = []

    def fake_steer_issue(
        endpoint: ControlEndpoint, issue_identifier: str, message: str
    ) -> dict[str, str]:
        calls.append((endpoint, issue_identifier, message))
        return {
            "issue_identifier": issue_identifier,
            "thread_id": "thread-1",
            "turn_id": "turn-1",
        }

    monkeypatch.setattr("code_factory.cli.steer_issue", fake_steer_issue)
    try:
        result = runner.invoke(
            typer.main.get_command(app), ["steer", "ENG-1", "focus on tests"]
        )
    finally:
        clear_runtime_metadata(str(workflow.resolve()))
    assert result.exit_code == 0
    assert "Steering accepted for ENG-1" in result.output
    assert calls == [(ControlEndpoint("127.0.0.1", 4555), "ENG-1", "focus on tests")]


def test_steer_command_surfaces_control_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("prompt\n", encoding="utf-8")

    def fake_steer_issue(
        endpoint: ControlEndpoint, issue_identifier: str, message: str
    ) -> dict[str, str]:
        raise ControlRequestError("issue_not_found", "missing", 404)

    monkeypatch.setattr("code_factory.cli.steer_issue", fake_steer_issue)
    result = runner.invoke(typer.main.get_command(app), ["steer", "ENG-404", "focus"])
    assert result.exit_code == 1
    assert "missing" in result.output


def test_issue_get_uses_workflow_defaults_and_human_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_workflow_file(tmp_path / "WORKFLOW.md", tracker={"project_slug": "project"})
    calls: list[tuple[str, tuple, dict]] = []

    class FakeOps:
        def __init__(self) -> None:
            self.closed = False

        async def read_issue(self, issue: str, **kwargs: object) -> dict[str, object]:
            calls.append(("read_issue", (issue,), kwargs))
            return {
                "issue": {
                    "identifier": issue,
                    "title": "Fix the thing",
                    "state": {"name": "In Progress"},
                }
            }

        async def close(self) -> None:
            self.closed = True

    fake_ops = FakeOps()
    monkeypatch.setattr(
        "code_factory.trackers.cli.build_tracker_ops",
        lambda _settings, *, allowed_roots: fake_ops,
    )

    result = runner.invoke(typer.main.get_command(app), ["issue", "get", "ENG-1"])
    assert result.exit_code == 0
    assert "ENG-1: Fix the thing [In Progress]" in result.output
    assert calls == [
        (
            "read_issue",
            ("ENG-1",),
            {
                "include_description": True,
                "include_comments": True,
                "include_attachments": True,
                "include_relations": True,
            },
        )
    ]


def test_workpad_sync_reads_repo_relative_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_workflow_file(tmp_path / "WORKFLOW.md", tracker={"project_slug": "project"})
    (tmp_path / "workpad.md").write_text("hello from cwd\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeOps:
        async def sync_workpad(
            self,
            issue: str,
            *,
            body: str | None = None,
            file_path: str | None = None,
        ) -> dict[str, object]:
            assert body is None
            assert file_path is not None
            captured["body"] = read_text_file(
                file_path,
                captured["allowed_roots"],  # type: ignore[arg-type]
            )
            captured["issue"] = issue
            return {"comment_id": "comment-1", "created": True}

        async def close(self) -> None:
            return None

    def fake_build_tracker_ops(_settings, *, allowed_roots):
        captured["allowed_roots"] = allowed_roots
        return FakeOps()

    monkeypatch.setattr(
        "code_factory.trackers.cli.build_tracker_ops",
        fake_build_tracker_ops,
    )

    result = runner.invoke(
        typer.main.get_command(app),
        ["workpad", "sync", "ENG-1", "--file", "workpad.md", "--json"],
    )
    assert result.exit_code == 0
    assert captured["issue"] == "ENG-1"
    assert captured["body"] == "hello from cwd\n"
    assert str(tmp_path.resolve()) in captured["allowed_roots"]  # type: ignore[operator]
