from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import click
import httpx
import pytest

from code_factory.application.dashboard.dashboard_workflow import project_link
from code_factory.application.project_links import resolve_project_url
from code_factory.errors import ConfigValidationError, TrackerClientError
from code_factory.trackers import cli_support
from code_factory.trackers.cli import _run_and_render
from code_factory.trackers.linear.bootstrap import LinearBootstrapper
from code_factory.trackers.linear.client import LinearClient
from code_factory.trackers.linear.ops.ops import LinearOps
from code_factory.trackers.linear.ops.ops_queries import ISSUES_QUERY, PROJECTS_QUERY
from code_factory.trackers.linear.project_resolution import (
    project_ambiguous_error,
    project_not_found_error,
    validate_config_project,
    validate_project_name,
)
from code_factory.trackers.user_errors import tracker_error_payload

from .conftest import make_snapshot, write_workflow_file


def make_settings(tmp_path: Path, *, tracker: dict[str, Any] | None = None):
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", tracker=tracker or {})
    return make_snapshot(workflow).settings


@pytest.mark.asyncio
async def test_project_name_resolution_helpers_cover_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    assert (
        project_link(
            {"tracker": {"project_url": "https://linear.app/project/live"}},
            "https://fallback",
        )
        == "https://linear.app/project/live"
    )
    assert (
        await resolve_project_url(
            make_settings(
                tmp_path,
                tracker={"kind": "memory", "api_key": None, "project": None},
            )
        )
        is None
    )

    closed: list[str] = []

    class SuccessBootstrapper:
        def __init__(self, *, api_key: str, endpoint: str) -> None:
            assert api_key == "token"
            assert endpoint == "https://api.linear.app/graphql"

        async def resolve_project(self, project: str) -> object:
            assert project == "project"
            return SimpleNamespace(url=" https://linear.app/project/demo ")

        async def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(
        "code_factory.application.project_links.importlib.import_module",
        lambda _name: SimpleNamespace(LinearBootstrapper=SuccessBootstrapper),
    )
    assert await resolve_project_url(settings) == "https://linear.app/project/demo"

    class ErrorBootstrapper(SuccessBootstrapper):
        async def resolve_project(self, project: str) -> object:
            raise TrackerClientError(("linear_api_status", 401))

    monkeypatch.setattr(
        "code_factory.application.project_links.importlib.import_module",
        lambda _name: SimpleNamespace(LinearBootstrapper=ErrorBootstrapper),
    )
    assert await resolve_project_url(settings) is None
    assert closed == ["closed", "closed"]


def test_project_name_validation_and_error_payload_helpers() -> None:
    validate_config_project({})
    validate_config_project({"tracker": []})
    with pytest.raises(ConfigValidationError, match="tracker.project_slug"):
        validate_config_project({"tracker": {"project_slug": "legacy"}})

    assert validate_project_name(" Demo ", config_error=True) == "Demo"
    assert validate_project_name("   ", config_error=True) == ""

    with pytest.raises(ConfigValidationError, match="tracker.project must be"):
        validate_project_name("linear.app/demo", config_error=True)
    with pytest.raises(TrackerClientError, match="tracker_invalid_project_reference"):
        validate_project_name("https://linear.app/demo", config_error=False)
    with pytest.raises(TrackerClientError, match="tracker_invalid_project_reference"):
        validate_project_name("linear.app/demo", config_error=False)
    with pytest.raises(TrackerClientError, match="tracker_invalid_project_reference"):
        validate_project_name("abcdef123456", config_error=False)
    assert validate_project_name("friendly-project", config_error=False) == (
        "friendly-project"
    )

    assert project_not_found_error("Demo").reason == (
        "tracker_project_not_found",
        "Demo",
    )
    assert project_ambiguous_error("Demo").reason == (
        "tracker_project_ambiguous",
        "Demo",
    )

    assert (
        "project name"
        in tracker_error_payload(
            TrackerClientError(("tracker_invalid_project_reference", "demo"))
        )["error"]["message"]
    )
    assert (
        '"Demo" was not found'
        in tracker_error_payload(
            TrackerClientError(("tracker_project_not_found", "Demo"))
        )["error"]["message"]
    )
    assert (
        "matches multiple projects"
        in tracker_error_payload(
            TrackerClientError(("tracker_project_ambiguous", "Demo"))
        )["error"]["message"]
    )


def test_tracker_cli_support_and_error_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run_ops = cli_support._run_ops
    rendered: list[str] = []

    async def fake_run_ops_issue(workflow: Any, callback: Any) -> dict[str, Any]:
        return {
            "issue": {"identifier": "ENG-1", "title": "Fix", "state": {"name": "Todo"}}
        }

    monkeypatch.setattr(cli_support, "_run_ops", fake_run_ops_issue)
    monkeypatch.setattr(cli_support.typer, "echo", lambda text: rendered.append(text))
    cli_support._run_and_render(None, False, lambda _ops: None)  # type: ignore[arg-type]
    assert rendered[-1] == "ENG-1: Fix [Todo]"

    async def fake_run_ops_json(workflow: Any, callback: Any) -> dict[str, Any]:
        return {"x": 1}

    monkeypatch.setattr(cli_support, "_run_ops", fake_run_ops_json)
    cli_support._run_and_render(None, True, lambda _ops: None)  # type: ignore[arg-type]
    assert '"x": 1' in rendered[-1]

    class FakeOps:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    fake_ops = FakeOps()
    monkeypatch.setattr(
        cli_support, "build_tracker_ops", lambda _settings, *, allowed_roots: fake_ops
    )
    monkeypatch.setattr(cli_support, "_run_ops", real_run_ops)
    monkeypatch.chdir(tmp_path)
    write_workflow_file(tmp_path / "WORKFLOW.md")

    async def callback(_ops: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(cli_support._run_ops(None, callback))
    assert fake_ops.closed is True

    console_calls: list[str] = []
    monkeypatch.setattr(
        cli_support.console, "print_json", lambda *, data: console_calls.append(data)
    )
    cli_support._render_human({"other": True})
    assert console_calls

    async def failing_support_run_ops(workflow: Any, callback: Any) -> dict[str, Any]:
        raise TrackerClientError(("tracker_operation_failed", "support boom"))

    monkeypatch.setattr(cli_support, "_run_ops", failing_support_run_ops)
    with pytest.raises(click.ClickException, match="support boom"):
        cli_support._run_and_render(None, False, lambda _ops: None)  # type: ignore[arg-type]

    async def failing_run_ops(workflow: Any, callback: Any) -> dict[str, Any]:
        raise TrackerClientError(("tracker_operation_failed", "boom"))

    monkeypatch.setattr("code_factory.trackers.cli._run_ops", failing_run_ops)
    with pytest.raises(click.ClickException, match="boom"):
        _run_and_render(None, False, lambda _ops: None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_linear_project_lookup_error_branches(tmp_path: Path) -> None:
    async def bootstrap_request(
        payload: dict[str, Any], _headers: list[tuple[str, str]]
    ) -> httpx.Response:
        if str(payload["query"]) == PROJECTS_QUERY:
            return httpx.Response(
                200, json={"data": {"projects": {"nodes": [{"id": "project-1"}]}}}
            )
        raise AssertionError(payload["query"])

    bootstrapper = LinearBootstrapper(api_key="token", request_fun=bootstrap_request)
    assert await bootstrapper._projects() == [{"id": "project-1"}]
    await bootstrapper.close()

    settings = make_settings(tmp_path)

    class UnknownProjectGraphQL:
        async def request(
            self,
            query: str,
            variables: dict[str, Any] | None = None,
            operation_name: str | None = None,
        ) -> dict[str, Any]:
            return {"data": {"projects": []}}

        async def close(self) -> None:
            return None

    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        await LinearClient(
            settings,
            client_factory=cast(Any, lambda: UnknownProjectGraphQL()),
        )._project()

    class MissingProjectGraphQL(UnknownProjectGraphQL):
        async def request(
            self,
            query: str,
            variables: dict[str, Any] | None = None,
            operation_name: str | None = None,
        ) -> dict[str, Any]:
            return {"data": {"projects": {"nodes": []}}}

    with pytest.raises(TrackerClientError, match="tracker_project_not_found"):
        await LinearClient(
            settings,
            client_factory=cast(Any, lambda: MissingProjectGraphQL()),
        )._project()

    class AmbiguousProjectGraphQL(UnknownProjectGraphQL):
        async def request(
            self,
            query: str,
            variables: dict[str, Any] | None = None,
            operation_name: str | None = None,
        ) -> dict[str, Any]:
            return {
                "data": {
                    "projects": {"nodes": [{"id": "project-1"}, {"id": "project-2"}]}
                }
            }

    with pytest.raises(TrackerClientError, match="tracker_project_ambiguous"):
        await LinearClient(
            settings,
            client_factory=cast(Any, lambda: AmbiguousProjectGraphQL()),
        )._project()

    async def missing_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"data": {"projects": {"nodes": []}}}

    with pytest.raises(TrackerClientError, match="tracker_project_not_found"):
        await LinearOps(settings, missing_graphql)._project_node("project")

    async def ambiguous_graphql(
        query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "data": {"projects": {"nodes": [{"id": "project-1"}, {"id": "project-2"}]}}
        }

    with pytest.raises(TrackerClientError, match="tracker_project_ambiguous"):
        await LinearOps(settings, ambiguous_graphql)._project_node("project")


@pytest.mark.asyncio
async def test_linear_read_issues_stops_when_limit_is_reached(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    issue_queries: list[dict[str, Any]] = []

    async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "CodeFactoryTrackerProjectByName" in query:
            return {"data": {"projects": {"nodes": [{"id": "project-1"}]}}}
        assert query == ISSUES_QUERY
        issue_queries.append(variables)
        return {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "issue-1",
                            "identifier": "ENG-1",
                            "title": "Fix",
                            "state": {"name": "Todo"},
                            "project": {"id": "project-1"},
                        }
                    ],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-2"},
                }
            }
        }

    ops = LinearOps(settings, graphql)
    payload = await ops.read_issues(
        project="project",
        state=None,
        query=None,
        limit=1,
        include_description=False,
        include_comments=False,
        include_attachments=False,
        include_relations=False,
    )
    assert payload["count"] == 1
    assert issue_queries == [
        {
            "first": 50,
            "after": None,
            "includeDescription": False,
            "includeComments": False,
            "includeAttachments": False,
            "includeRelations": False,
        }
    ]
