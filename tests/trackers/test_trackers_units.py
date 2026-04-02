from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from code_factory.errors import ConfigValidationError, TrackerClientError
from code_factory.trackers.base import (
    build_tracker,
    parse_tracker_settings,
    validate_tracker_settings,
)
from code_factory.trackers.bootstrap import build_tracker_bootstrapper
from code_factory.trackers.linear import LinearOps
from code_factory.trackers.linear.bootstrap import LinearBootstrapper
from code_factory.trackers.linear.bootstrap import _data as bootstrap_data
from code_factory.trackers.linear.bootstrap import _nodes as bootstrap_nodes
from code_factory.trackers.linear.client import LinearClient
from code_factory.trackers.linear.config import (
    parse_tracker_settings as parse_linear_tracker_settings,
)
from code_factory.trackers.linear.config import (
    supports_tracker_kind,
)
from code_factory.trackers.linear.config import (
    validate_tracker_settings as validate_linear_tracker_settings,
)
from code_factory.trackers.linear.decoding import (
    assigned_to_worker,
    assignee_id,
    decode_comments_page_response,
    decode_linear_page_response,
    decode_linear_response,
    decode_nodes,
    extract_blockers,
    extract_labels,
    next_page_cursor,
    normalize_issue,
    parse_datetime,
    string_or_none,
)
from code_factory.trackers.linear.graphql import (
    LinearGraphQLClient,
    summarize_error_body,
)
from code_factory.trackers.linear.ops.ops_queries import (
    ATTACH_LINK_FALLBACK_MUTATION,
    ATTACH_PR_MUTATION,
    CREATE_ISSUE_MUTATION,
    CREATE_RELATION_MUTATION,
    FILE_UPLOAD_MUTATION,
    ISSUES_QUERY,
    PROJECTS_QUERY,
    TEAMS_QUERY,
)
from code_factory.trackers.linear.queries import (
    COMMENTS_QUERY,
    CREATE_COMMENT_MUTATION,
    QUERY,
    QUERY_BY_IDENTIFIER,
    QUERY_BY_IDS,
    STATE_LOOKUP_QUERY,
    UPDATE_COMMENT_MUTATION,
    UPDATE_STATE_MUTATION,
    VIEWER_QUERY,
)
from code_factory.trackers.memory import MemoryTracker
from code_factory.trackers.memory.tracker import build_tracker as build_memory_tracker

from ..conftest import make_issue, make_snapshot, write_workflow_file


def make_settings(tmp_path: Path, *, tracker: dict[str, Any] | None = None):
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", tracker=tracker or {})
    return make_snapshot(workflow).settings


def test_tracker_base_build_validate_and_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_settings = make_settings(
        tmp_path, tracker={"kind": "memory", "api_key": None, "project": None}
    )
    linear_settings = make_settings(tmp_path)

    monkeypatch.setattr(
        "code_factory.trackers.memory.tracker.build_tracker",
        lambda settings, **kwargs: ("memory", settings, kwargs),
    )
    monkeypatch.setattr(
        "code_factory.trackers.linear.client.build_tracker",
        lambda settings, **kwargs: ("linear", settings, kwargs),
    )
    assert cast(Any, build_tracker(memory_settings, sample=True))[0] == "memory"
    assert cast(Any, build_tracker(linear_settings, sample=True))[0] == "linear"

    with pytest.raises(ConfigValidationError, match="tracker.kind is required"):
        validate_tracker_settings(
            make_settings(
                tmp_path, tracker={"kind": None, "api_key": "t", "project": "p"}
            )
        )
    validate_tracker_settings(memory_settings)
    with pytest.raises(ConfigValidationError, match="unsupported tracker kind"):
        validate_tracker_settings(
            make_settings(
                tmp_path, tracker={"kind": "jira", "api_key": "t", "project": "p"}
            )
        )

    parse_calls: list[Any] = []
    validate_calls: list[Any] = []
    monkeypatch.setattr(
        "code_factory.trackers.linear.config.validate_tracker_settings",
        lambda settings: validate_calls.append(settings),
    )
    validate_tracker_settings(linear_settings)
    assert validate_calls == [linear_settings]

    tracker = parse_tracker_settings(
        {"tracker": {"kind": "memory"}, "states": {"Todo": {"prompt": "default"}}}
    )
    assert tracker.kind == "memory"
    parsed_linear_tracker = linear_settings.tracker

    monkeypatch.setattr(
        "code_factory.trackers.linear.config.parse_tracker_settings",
        lambda config: parse_calls.append(config) or parsed_linear_tracker,
    )
    parse_tracker_settings(
        {"tracker": {"kind": "linear"}, "states": {"Todo": {"prompt": "default"}}}
    )
    assert parse_calls


def test_linear_config_defaults_env_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "env-token")
    monkeypatch.setenv("LINEAR_ASSIGNEE", "user-1")
    tracker = parse_linear_tracker_settings(
        {"tracker": {"kind": "linear"}, "states": {"Todo": {"prompt": "default"}}}
    )
    assert tracker.kind == "linear"
    assert tracker.api_key == "env-token"
    assert tracker.assignee == "user-1"
    assert supports_tracker_kind("linear") is True
    assert supports_tracker_kind("memory") is False

    monkeypatch.delenv("LINEAR_API_KEY")
    with pytest.raises(ConfigValidationError, match="LINEAR_API_KEY is required"):
        validate_linear_tracker_settings(
            make_settings(tmp_path, tracker={"api_key": None, "project": "p"})
        )
    with pytest.raises(ConfigValidationError, match="tracker.project is required"):
        validate_linear_tracker_settings(
            make_settings(tmp_path, tracker={"project": None})
        )


def test_linear_decoding_behaviors() -> None:
    issue_node = {
        "id": "issue-1",
        "identifier": "ENG-1",
        "title": "Fix thing",
        "description": "desc",
        "priority": 2,
        "state": {"name": "In Progress"},
        "branchName": "codex/eng-1",
        "url": "https://example/ENG-1",
        "assignee": {"id": " user-1 "},
        "labels": {"nodes": [{"name": "Backend"}, {"name": "Ops"}]},
        "inverseRelations": {
            "nodes": [
                {
                    "type": "blocks",
                    "issue": {
                        "id": "blocker-1",
                        "identifier": "ENG-0",
                        "state": {"name": "Todo"},
                    },
                },
                {"type": "relates", "issue": {"id": "ignored"}},
            ]
        },
        "createdAt": "2024-01-02T03:04:05Z",
        "updatedAt": "2024-01-02T04:05:06Z",
    }

    normalized = normalize_issue(issue_node, {"match_values": {"user-1"}})
    assert normalized is not None
    assert normalized.identifier == "ENG-1"
    assert normalized.assigned_to_worker is True
    assert normalized.labels == ("backend", "ops")
    assert normalized.blocked_by[0].identifier == "ENG-0"
    assert normalize_issue({}, None) is not None
    assert normalize_issue(cast(Any, []), None) is None
    assert assigned_to_worker({}, None) is True
    assert assigned_to_worker({"id": "user-2"}, {"match_values": {"user-1"}}) is False
    assert assignee_id({"id": " user-1 "}) == "user-1"
    assert assignee_id({"id": " "}) is None
    assert extract_labels({"labels": {"nodes": [{"name": "One"}, {"name": 2}]}}) == [
        "one"
    ]
    assert extract_labels({}) == []
    assert extract_blockers(issue_node)[0].id == "blocker-1"
    assert extract_blockers({}) == []
    assert parse_datetime("2024-01-02T03:04:05Z") == datetime(
        2024, 1, 2, 3, 4, 5, tzinfo=UTC
    )
    assert parse_datetime("not-a-date") is None
    assert string_or_none("x") == "x"
    assert string_or_none(1) is None

    page = {
        "data": {
            "issues": {
                "nodes": [issue_node, "bad"],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
            }
        }
    }
    issues, page_info = decode_linear_page_response(page, None)
    assert len(issues) == 1
    assert page_info == {"has_next_page": True, "end_cursor": "cursor-1"}
    assert decode_linear_response(page, None)[0].identifier == "ENG-1"
    assert decode_nodes([issue_node, "bad"], None)[0].identifier == "ENG-1"
    assert next_page_cursor(page_info) == "cursor-1"
    assert next_page_cursor({"has_next_page": False, "end_cursor": None}) is None

    with pytest.raises(TrackerClientError, match="linear_missing_end_cursor"):
        next_page_cursor({"has_next_page": True, "end_cursor": None})
    with pytest.raises(TrackerClientError, match="linear_graphql_errors"):
        decode_linear_page_response({"errors": ["boom"]}, None)
    with pytest.raises(TrackerClientError, match="linear_graphql_errors"):
        decode_comments_page_response({"errors": ["boom"]})
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        decode_linear_response({"data": {}}, None)
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        decode_comments_page_response({"data": {"issue": "bad"}})
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        decode_comments_page_response({"data": {"issue": {"comments": "bad"}}})
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        decode_comments_page_response(
            {"data": {"issue": {"comments": {"nodes": [], "pageInfo": []}}}}
        )


@pytest.mark.asyncio
async def test_linear_graphql_client_request_and_summary(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seen: list[tuple[dict[str, Any], list[tuple[str, str]]]] = []

    async def request_ok(
        payload: dict[str, Any], headers: list[tuple[str, str]]
    ) -> httpx.Response:
        seen.append((payload, headers))
        return httpx.Response(200, json={"data": {"viewer": {"id": "usr"}}})

    client = LinearGraphQLClient(settings, request_fun=request_ok)
    result = await client.request("query Viewer { viewer { id } }", {}, " Viewer ")
    assert result == {"data": {"viewer": {"id": "usr"}}}
    assert seen[0][0]["operationName"] == "Viewer"
    assert ("Authorization", settings.tracker.api_key or "") in seen[0][1]
    await client.close()

    async def request_status(
        payload: dict[str, Any], headers: list[tuple[str, str]]
    ) -> httpx.Response:
        return httpx.Response(503, json={"error": "bad"})

    with pytest.raises(TrackerClientError, match="linear_api_status"):
        await LinearGraphQLClient(settings, request_fun=request_status).request(
            "query", {}
        )

    async def request_invalid(
        payload: dict[str, Any], headers: list[tuple[str, str]]
    ) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        await LinearGraphQLClient(settings, request_fun=request_invalid).request(
            "query", {}
        )

    async def request_http_error(
        payload: dict[str, Any], headers: list[tuple[str, str]]
    ) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(TrackerClientError, match="linear_api_request"):
        await LinearGraphQLClient(settings, request_fun=request_http_error).request(
            "query", {}
        )

    long_body = httpx.Response(500, json={"error": "x" * 2_000})
    assert summarize_error_body(long_body).endswith("...<truncated>")
    assert (
        summarize_error_body(httpx.Response(500, content=b"plain text")) == "plain text"
    )


@pytest.mark.asyncio
async def test_linear_bootstrapper_resolves_projects_and_creates_missing_states() -> (
    None
):
    requests: list[dict[str, Any]] = []

    async def request_fun(
        payload: dict[str, Any], headers: list[tuple[str, str]]
    ) -> httpx.Response:
        requests.append(payload)
        assert ("Authorization", "token") in headers
        query = str(payload["query"])
        if "CodeFactoryTrackerProjectByName" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "projects": {
                            "nodes": [
                                {
                                    "id": "project-1",
                                    "name": "Demo Project",
                                    "slugId": "demo-project",
                                    "teams": {
                                        "nodes": [
                                            {
                                                "id": "team-1",
                                                "name": "Engineering",
                                                "key": "ENG",
                                                "states": {
                                                    "nodes": [
                                                        {
                                                            "id": "state-1",
                                                            "name": "Todo",
                                                            "type": "unstarted",
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                },
            )
        if query == TEAMS_QUERY:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "teams": {
                            "nodes": [
                                {
                                    "id": "team-1",
                                    "name": "Engineering",
                                    "key": "ENG",
                                    "states": {
                                        "nodes": [
                                            {
                                                "id": "state-1",
                                                "name": "Todo",
                                                "type": "unstarted",
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                },
            )
        if "workflowStateCreate" in query:
            state_name = payload["variables"]["input"]["name"]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "workflowStateCreate": {
                            "success": True,
                            "workflowState": {
                                "id": f"{state_name}-id",
                                "name": state_name,
                                "type": payload["variables"]["input"]["type"],
                            },
                        }
                    }
                },
            )
        raise AssertionError(query)

    bootstrapper = LinearBootstrapper(api_key="token", request_fun=request_fun)
    by_slug = await bootstrapper.resolve_project("Demo Project")
    project = await bootstrapper.resolve_project("Demo Project")
    assert by_slug is not None
    assert by_slug.slug_id == "demo-project"
    assert project is not None
    assert project.slug_id == "demo-project"

    created = await bootstrapper.ensure_states(
        team=project.teams[0],
        required_states=(
            ("Todo", "unstarted"),
            ("Human Review", "started"),
            ("Done", "completed"),
        ),
    )

    assert [state.name for state in created] == ["Human Review", "Done"]
    assert requests[0]["variables"] == {"name": "Demo Project", "first": 10}
    assert requests[1]["variables"] == {"first": 100}
    await bootstrapper.close()


@pytest.mark.asyncio
async def test_linear_bootstrapper_creates_project_and_resolves_team() -> None:
    requests: list[dict[str, Any]] = []

    async def request_fun(
        payload: dict[str, Any], _headers: list[tuple[str, str]]
    ) -> httpx.Response:
        requests.append(payload)
        query = str(payload["query"])
        if query == TEAMS_QUERY:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "teams": {
                            "nodes": [
                                {
                                    "id": "team-1",
                                    "name": "Engineering",
                                    "key": "ENG",
                                    "states": {"nodes": []},
                                }
                            ]
                        }
                    }
                },
            )
        if "projectCreate" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "projectCreate": {
                            "success": True,
                            "project": {
                                "id": "project-1",
                                "name": "Demo Project",
                                "slugId": "demo-project-1",
                                "teams": {
                                    "nodes": [
                                        {
                                            "id": "team-1",
                                            "name": "Engineering",
                                            "key": "ENG",
                                            "states": {"nodes": []},
                                        }
                                    ]
                                },
                            },
                        }
                    }
                },
            )
        raise AssertionError(query)

    bootstrapper = LinearBootstrapper(api_key="token", request_fun=request_fun)
    team = await bootstrapper.resolve_team("ENG")
    project = await bootstrapper.create_project(name="Demo Project", team=team)

    assert team.key == "ENG"
    assert project.slug_id == "demo-project-1"
    assert requests[1]["variables"]["input"] == {
        "name": "Demo Project",
        "teamIds": ["team-1"],
    }
    await bootstrapper.close()


@pytest.mark.asyncio
async def test_linear_bootstrapper_error_paths_and_generic_factory() -> None:
    assert isinstance(
        build_tracker_bootstrapper(tracker_kind="linear", api_key="token"),
        LinearBootstrapper,
    )
    with pytest.raises(ValueError, match="unsupported tracker bootstrap kind"):
        build_tracker_bootstrapper(tracker_kind="memory", api_key="token")

    async def ambiguous_projects(
        payload: dict[str, Any], _headers: list[tuple[str, str]]
    ) -> httpx.Response:
        query = str(payload["query"])
        if "CodeFactoryTrackerProjectByName" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "projects": {
                            "nodes": [
                                {
                                    "id": "project-1",
                                    "name": "Demo",
                                    "slugId": "demo",
                                    "teams": {"nodes": []},
                                },
                                {
                                    "id": "project-2",
                                    "name": "Demo",
                                    "slugId": "other",
                                    "teams": {"nodes": []},
                                },
                            ]
                        }
                    }
                },
            )
        raise AssertionError(query)

    with pytest.raises(TrackerClientError, match="tracker_project_ambiguous"):
        await LinearBootstrapper(
            api_key="token", request_fun=ambiguous_projects
        ).resolve_project("demo")

    async def ambiguous_name_projects(
        payload: dict[str, Any], _headers: list[tuple[str, str]]
    ) -> httpx.Response:
        if "CodeFactoryTrackerProjectByName" in str(payload["query"]):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "projects": {
                            "nodes": [
                                {
                                    "id": "project-1",
                                    "name": "Demo",
                                    "slugId": "demo-one",
                                    "teams": {"nodes": []},
                                },
                                {
                                    "id": "project-2",
                                    "name": "Demo",
                                    "slugId": "demo-two",
                                    "teams": {"nodes": []},
                                },
                            ]
                        }
                    }
                },
            )
        raise AssertionError(payload["query"])

    with pytest.raises(TrackerClientError, match="tracker_project_ambiguous"):
        await LinearBootstrapper(
            api_key="token", request_fun=ambiguous_name_projects
        ).resolve_project("Demo")

    async def missing_project(
        payload: dict[str, Any], _headers: list[tuple[str, str]]
    ) -> httpx.Response:
        if "CodeFactoryTrackerProjectByName" in str(payload["query"]):
            return httpx.Response(200, json={"data": {"projects": {"nodes": []}}})
        raise AssertionError(payload["query"])

    assert (
        await LinearBootstrapper(
            api_key="token", request_fun=missing_project
        ).resolve_project("missing")
        is None
    )

    async def missing_team(
        payload: dict[str, Any], _headers: list[tuple[str, str]]
    ) -> httpx.Response:
        if str(payload["query"]) == TEAMS_QUERY:
            return httpx.Response(200, json={"data": {"teams": {"nodes": []}}})
        raise AssertionError(payload["query"])

    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        await LinearBootstrapper(
            api_key="token", request_fun=missing_team
        ).resolve_team("ENG")

    failing_team = {
        "id": "team-1",
        "name": "Engineering",
        "key": "ENG",
        "states": {"nodes": []},
    }

    async def mutation_failures(
        payload: dict[str, Any], _headers: list[tuple[str, str]]
    ) -> httpx.Response:
        query = str(payload["query"])
        if "projectCreate" in query:
            return httpx.Response(
                200, json={"data": {"projectCreate": {"success": False}}}
            )
        if "workflowStateCreate" in query:
            return httpx.Response(
                200,
                json={"data": {"workflowStateCreate": {"success": False}}},
            )
        raise AssertionError(query)

    bootstrapper = LinearBootstrapper(api_key="token", request_fun=mutation_failures)
    team = await LinearBootstrapper(
        api_key="token",
        request_fun=lambda payload, _headers: asyncio.sleep(
            0,
            result=httpx.Response(
                200, json={"data": {"teams": {"nodes": [failing_team]}}}
            ),
        ),
    ).resolve_team("ENG")
    with pytest.raises(TrackerClientError, match="tracker project creation failed"):
        await bootstrapper.create_project(name="Demo", team=team)
    with pytest.raises(
        TrackerClientError, match="tracker workflow state creation failed"
    ):
        await bootstrapper.ensure_states(
            team=team,
            required_states=(("Todo", "unstarted"),),
        )

    async def invalid_mutation_payloads(
        payload: dict[str, Any], _headers: list[tuple[str, str]]
    ) -> httpx.Response:
        query = str(payload["query"])
        if "projectCreate" in query:
            return httpx.Response(
                200,
                json={"data": {"projectCreate": {"success": True, "project": []}}},
            )
        if "workflowStateCreate" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "workflowStateCreate": {
                            "success": True,
                            "workflowState": [],
                        }
                    }
                },
            )
        raise AssertionError(query)

    invalid_bootstrapper = LinearBootstrapper(
        api_key="token", request_fun=invalid_mutation_payloads
    )
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        await invalid_bootstrapper.create_project(name="Demo", team=team)
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        await invalid_bootstrapper.ensure_states(
            team=team,
            required_states=(("Todo", "unstarted"),),
        )

    with pytest.raises(TrackerClientError, match="tracker_operation_failed"):
        bootstrap_data({"errors": [{"message": "boom"}]}, "projects")
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        bootstrap_data({"data": {"projects": []}}, "projects")
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        bootstrap_nodes({"nodes": "bad"})


@pytest.mark.asyncio
async def test_memory_tracker_behaviors() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    issue_a = make_issue(id="a", state="Todo")
    issue_b = make_issue(id="b", identifier="ENG-2", state="Done")
    tracker = MemoryTracker([issue_a, issue_b], recipient=queue)
    tracker.replace_issues([issue_a, issue_b])

    assert await tracker.fetch_candidate_issues() == [issue_a, issue_b]
    assert await tracker.fetch_issues_by_states(["todo"]) == [issue_a]
    assert await tracker.fetch_issue_states_by_ids(["b"]) == [issue_b]

    await tracker.create_comment("a", "hello")
    comment = (await tracker.fetch_issue_comments("a"))[0]
    assert comment.body == "hello"
    assert comment.id is not None
    with pytest.raises(TrackerClientError, match="comment_update_failed"):
        await tracker.update_comment("missing", "ignored")
    await tracker.update_comment(comment.id, "updated")
    assert (await tracker.fetch_issue_comments("a"))[0].body == "updated"
    await tracker.update_issue_state("a", "In Progress")
    assert (await tracker.fetch_issue_states_by_ids(["a"]))[0].state == "In Progress"
    assert await queue.get() == ("memory_tracker_comment", "a", "hello")
    assert await queue.get() == ("memory_tracker_state_update", "a", "In Progress")
    assert isinstance(build_memory_tracker(None), MemoryTracker)


@pytest.mark.asyncio
async def test_linear_client_core_behaviors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeGraphQL:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

        async def request(
            self,
            query: str,
            variables: dict[str, Any] | None = None,
            operation_name: str | None = None,
        ) -> dict[str, Any]:
            payload = variables or {}
            calls.append((query, payload))
            if query == VIEWER_QUERY:
                return {"data": {"viewer": {"id": "viewer-1"}}}
            if "CodeFactoryTrackerProjectByName" in query:
                return {
                    "data": {
                        "projects": {
                            "nodes": [
                                {
                                    "id": "project-1",
                                    "name": "project",
                                    "slugId": "project",
                                    "url": "https://linear.app/project/project",
                                }
                            ]
                        }
                    }
                }
            if query == QUERY:
                after = payload.get("after")
                node = {
                    "id": "issue-1" if after is None else "issue-2",
                    "identifier": "ENG-1" if after is None else "ENG-2",
                    "title": "Issue",
                    "state": {"name": "Todo"},
                    "assignee": {"id": "viewer-1"},
                }
                return {
                    "data": {
                        "issues": {
                            "nodes": [node],
                            "pageInfo": {
                                "hasNextPage": after is None,
                                "endCursor": "cursor-2" if after is None else None,
                            },
                        }
                    }
                }
            if query == QUERY_BY_IDS:
                nodes = [
                    {
                        "id": issue_id,
                        "identifier": issue_id.upper(),
                        "title": issue_id,
                        "state": {"name": "Todo"},
                        "assignee": {"id": "viewer-1"},
                    }
                    for issue_id in reversed(payload["ids"])
                ]
                return {"data": {"issues": {"nodes": nodes}}}
            if query == QUERY_BY_IDENTIFIER:
                identifier = payload["identifier"]
                if identifier == "ENG-404":
                    return {"data": {"issue": None}}
                return {
                    "data": {
                        "issue": {
                            "id": "issue-identifier",
                            "identifier": identifier,
                            "title": "By identifier",
                            "state": {"name": "Todo"},
                            "assignee": {"id": "viewer-1"},
                        }
                    }
                }
            if query == COMMENTS_QUERY:
                after = payload.get("after")
                return {
                    "data": {
                        "issue": {
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "comment-1"
                                        if after is None
                                        else "comment-2",
                                        "body": "result body",
                                        "createdAt": "2024-01-01T00:00:00Z",
                                        "updatedAt": "2024-01-01T00:00:00Z",
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": after is None,
                                    "endCursor": "cursor-2" if after is None else None,
                                },
                            }
                        }
                    }
                }
            if query == CREATE_COMMENT_MUTATION:
                return {"data": {"commentCreate": {"success": payload["body"] == "ok"}}}
            if query == UPDATE_COMMENT_MUTATION:
                return {
                    "data": {
                        "commentUpdate": {
                            "success": payload["commentId"] == "comment-1"
                        }
                    }
                }
            if query == STATE_LOOKUP_QUERY:
                return {
                    "data": {
                        "issue": {"team": {"states": {"nodes": [{"id": "state-1"}]}}}
                    }
                }
            if query == UPDATE_STATE_MUTATION:
                return {
                    "data": {
                        "issueUpdate": {"success": payload["stateId"] == "state-1"}
                    }
                }
            raise AssertionError(f"unexpected query: {query}")

    fake_graphql = FakeGraphQL()
    client = LinearClient(settings, client_factory=cast(Any, lambda: fake_graphql))

    assert [issue.id for issue in await client.fetch_candidate_issues()] == [
        "issue-1",
        "issue-2",
    ]
    assert [
        issue.id for issue in await client.fetch_issues_by_states(["Todo", "Todo"])
    ] == [
        "issue-1",
        "issue-2",
    ]
    assert [
        issue.id for issue in await client.fetch_issue_states_by_ids(["b", "a", "b"])
    ] == [
        "b",
        "a",
    ]
    identified = await client.fetch_issue_by_identifier("ENG-24")
    assert identified is not None
    assert identified.id == "issue-identifier"
    assert identified.identifier == "ENG-24"
    assert await client.fetch_issue_by_identifier("ENG-404") is None
    assert [comment.id for comment in await client.fetch_issue_comments("issue-1")] == [
        "comment-1",
        "comment-2",
    ]
    await client.create_comment("issue-1", "ok")
    with pytest.raises(TrackerClientError, match="comment_create_failed"):
        await client.create_comment("issue-1", "bad")
    await client.update_comment("comment-1", "updated")
    with pytest.raises(TrackerClientError, match="comment_update_failed"):
        await client.update_comment("missing", "updated")
    await client.update_issue_state("issue-1", "Done")
    await client.close()
    assert fake_graphql.closed is True
    assert any(query == QUERY for query, _payload in calls)

    custom_settings = make_settings(tmp_path, tracker={"assignee": "user-2"})
    custom_client = LinearClient(
        custom_settings, client_factory=cast(Any, lambda: FakeGraphQL())
    )
    assert await custom_client._routing_assignee_filter() == {
        "configured_assignee": "user-2",
        "match_values": {"user-2"},
    }

    me_settings = make_settings(tmp_path, tracker={"assignee": "me"})
    me_client = LinearClient(
        me_settings, client_factory=cast(Any, lambda: FakeGraphQL())
    )
    assert await me_client._routing_assignee_filter() == {
        "configured_assignee": "me",
        "match_values": {"viewer-1"},
    }

    missing_settings = make_settings(tmp_path, tracker={"api_key": ""})
    missing_client = LinearClient(
        missing_settings, client_factory=cast(Any, lambda: FakeGraphQL())
    )
    with pytest.raises(TrackerClientError, match="missing_linear_api_token"):
        await missing_client.fetch_candidate_issues()

    missing_slug_settings = make_settings(tmp_path, tracker={"project": None})
    missing_slug_client = LinearClient(
        missing_slug_settings, client_factory=cast(Any, lambda: FakeGraphQL())
    )
    with pytest.raises(TrackerClientError, match="missing_linear_project"):
        await missing_slug_client.fetch_candidate_issues()

    class BrokenGraphQL:
        async def request(
            self,
            query: str,
            variables: dict[str, Any] | None = None,
            operation_name: str | None = None,
        ) -> dict[str, Any]:
            return {"data": {"viewer": {}}}

        async def close(self) -> None:
            return None

    broken = LinearClient(
        make_settings(tmp_path, tracker={"assignee": "me"}),
        client_factory=cast(Any, lambda: BrokenGraphQL()),
    )
    with pytest.raises(TrackerClientError, match="missing_linear_viewer_identity"):
        await broken._routing_assignee_filter()


@pytest.mark.asyncio
async def test_linear_ops_read_issues_paginates_before_filtering(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    calls: list[dict[str, Any]] = []

    async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "CodeFactoryTrackerProjectByName" in query:
            return {
                "data": {
                    "projects": {
                        "nodes": [
                            {
                                "id": "project-1",
                                "name": "project",
                                "slugId": "project",
                                "url": "https://example/project-1",
                                "teams": {
                                    "nodes": [
                                        {
                                            "id": "team-1",
                                            "name": "Team",
                                            "key": "ENG",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        assert query == ISSUES_QUERY
        calls.append(variables)
        if variables.get("after") is None:
            return {
                "data": {
                    "issues": {
                        "nodes": [
                            {
                                "id": "issue-1",
                                "identifier": "ENG-1",
                                "title": "Ignore me",
                                "priority": 0,
                                "url": "https://example/ENG-1",
                                "branchName": "codex/eng-1",
                                "state": {
                                    "id": "state-1",
                                    "name": "Todo",
                                    "type": "unstarted",
                                },
                                "project": {
                                    "id": "project-2",
                                    "name": "Other",
                                    "slugId": "other",
                                    "url": "https://example/project-2",
                                },
                                "team": {"id": "team-1", "name": "Team", "key": "ENG"},
                                "labels": {"nodes": []},
                            }
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-2"},
                    }
                }
            }
        return {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "issue-2",
                            "identifier": "ENG-2",
                            "title": "Backlog item",
                            "priority": 0,
                            "url": "https://example/ENG-2",
                            "branchName": "codex/eng-2",
                            "state": {
                                "id": "state-2",
                                "name": "Backlog",
                                "type": "backlog",
                            },
                            "project": {
                                "id": "project-1",
                                "name": "Project",
                                "slugId": "project",
                                "url": "https://example/project-1",
                            },
                            "team": {"id": "team-1", "name": "Team", "key": "ENG"},
                            "labels": {"nodes": []},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }

    ops = LinearOps(settings, graphql)
    payload = await ops.read_issues(
        project="project",
        state="Backlog",
        query=None,
        limit=1,
        include_description=False,
        include_comments=False,
        include_attachments=False,
        include_relations=False,
    )
    assert [issue["identifier"] for issue in payload["issues"]] == ["ENG-2"]
    assert [call.get("after") for call in calls] == [None, "cursor-2"]


@pytest.mark.asyncio
async def test_linear_ops_create_issue_surfaces_relation_failures(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)

    class FakeLinearOps(LinearOps):
        async def _resolve_issue_target(
            self, project: object, team: object
        ) -> tuple[dict | None, dict | None]:
            return {"id": "team-1"}, None

        async def _issue_input(
            self,
            values: dict[str, object],
            *,
            team_node: dict | None,
            project_node: dict | None,
            issue_node: dict | None,
        ) -> dict:
            return {"title": values.get("title")}

        async def _resolve_issue_id(self, issue: str) -> str:
            return issue

    async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if query == CREATE_ISSUE_MUTATION:
            return {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "ENG-1",
                            "identifier": "ENG-1",
                            "title": "Issue",
                            "url": "https://example/ENG-1",
                        },
                    }
                }
            }
        if query == CREATE_RELATION_MUTATION:
            return {"data": {"issueRelationCreate": {"success": False}}}
        raise AssertionError(f"unexpected query: {query}")

    with pytest.raises(TrackerClientError, match="tracker relation update failed"):
        await FakeLinearOps(settings, graphql).create_issue(
            title="Issue",
            blocked_by=["ENG-2"],
        )


@pytest.mark.asyncio
async def test_linear_ops_link_pr_uses_attachment_create_fallback(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)

    class FakeLinearOps(LinearOps):
        async def _resolve_issue_id(self, issue: str) -> str:
            return issue

    async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if query == ATTACH_PR_MUTATION:
            return {"data": {"attachmentLinkGitHubPR": {"success": False}}}
        if query == ATTACH_LINK_FALLBACK_MUTATION:
            return {
                "data": {
                    "attachmentCreate": {
                        "success": True,
                        "attachment": {
                            "id": "attachment-1",
                            "title": variables["title"],
                            "url": variables["url"],
                        },
                    }
                }
            }
        raise AssertionError(f"unexpected query: {query}")

    result = await FakeLinearOps(settings, graphql).link_pr(
        "ENG-1",
        "https://github.com/org/repo/pull/1",
        "PR",
    )
    assert result == {
        "issue_id": "ENG-1",
        "url": "https://github.com/org/repo/pull/1",
        "title": "PR",
        "linked": True,
    }


@pytest.mark.asyncio
async def test_linear_ops_link_pr_surfaces_failed_fallback(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    class FakeLinearOps(LinearOps):
        async def _resolve_issue_id(self, issue: str) -> str:
            return issue

    async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if query == ATTACH_PR_MUTATION:
            return {"data": {"attachmentLinkGitHubPR": {"success": False}}}
        if query == ATTACH_LINK_FALLBACK_MUTATION:
            return {
                "data": {"attachmentCreate": {"success": False, "attachment": None}}
            }
        raise AssertionError(f"unexpected query: {query}")

    with pytest.raises(TrackerClientError, match="tracker PR link attachment failed"):
        await FakeLinearOps(settings, graphql).link_pr(
            "ENG-1",
            "https://github.com/org/repo/pull/1",
            "PR",
        )


@pytest.mark.asyncio
async def test_linear_ops_upload_file_sets_documented_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png-bytes")

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.calls: list[tuple[str, dict[str, str], bytes]] = []

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def put(
            self, url: str, *, headers: dict[str, str], content: bytes
        ) -> httpx.Response:
            self.calls.append((url, headers, content))
            return httpx.Response(
                200,
                request=httpx.Request("PUT", url),
            )

    http_client = FakeAsyncClient()
    monkeypatch.setattr(
        "code_factory.trackers.linear.ops.ops_write.httpx.AsyncClient",
        lambda **kwargs: http_client,
    )

    async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert query == FILE_UPLOAD_MUTATION
        assert variables == {
            "filename": "image.png",
            "contentType": "image/png",
            "size": 9,
        }
        return {
            "data": {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "uploadUrl": "https://upload.example/file",
                        "assetUrl": "https://asset.example/file",
                        "headers": [{"key": "x-ms-blob-type", "value": "BlockBlob"}],
                    },
                }
            }
        }

    result = await LinearOps(
        settings,
        graphql,
        allowed_roots=(str(tmp_path),),
    ).upload_file("image.png")
    assert result == {
        "filename": "image.png",
        "content_type": "image/png",
        "asset_url": "https://asset.example/file",
        "markdown": "![image.png](https://asset.example/file)",
    }
    assert http_client.calls == [
        (
            "https://upload.example/file",
            {
                "Content-Type": "image/png",
                "Cache-Control": "public, max-age=31536000",
                "x-ms-blob-type": "BlockBlob",
            },
            b"png-bytes",
        )
    ]


@pytest.mark.asyncio
async def test_linear_ops_upload_file_surfaces_put_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    file_path = tmp_path / "evidence.jpg"
    file_path.write_bytes(b"jpeg-bytes")

    class FailingAsyncClient:
        async def __aenter__(self) -> FailingAsyncClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def put(
            self, url: str, *, headers: dict[str, str], content: bytes
        ) -> httpx.Response:
            return httpx.Response(
                400,
                text="bad upload",
                request=httpx.Request("PUT", url),
            )

    monkeypatch.setattr(
        "code_factory.trackers.linear.ops.ops_write.httpx.AsyncClient",
        lambda **kwargs: FailingAsyncClient(),
    )

    async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert query == FILE_UPLOAD_MUTATION
        return {
            "data": {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "uploadUrl": "https://upload.example/file",
                        "assetUrl": "https://asset.example/file",
                        "headers": [],
                    },
                }
            }
        }

    with pytest.raises(
        TrackerClientError,
        match="tracker file upload PUT failed with HTTP 400: bad upload",
    ):
        await LinearOps(
            settings,
            graphql,
            allowed_roots=(str(tmp_path),),
        ).upload_file("evidence.jpg")
