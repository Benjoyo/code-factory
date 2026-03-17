from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from symphony.errors import ConfigValidationError, TrackerClientError
from symphony.trackers.base import (
    build_tracker,
    parse_tracker_settings,
    validate_tracker_settings,
)
from symphony.trackers.linear.client import LinearClient
from symphony.trackers.linear.config import (
    parse_tracker_settings as parse_linear_tracker_settings,
)
from symphony.trackers.linear.config import (
    supports_tracker_kind,
)
from symphony.trackers.linear.config import (
    validate_tracker_settings as validate_linear_tracker_settings,
)
from symphony.trackers.linear.decoding import (
    assigned_to_worker,
    assignee_id,
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
from symphony.trackers.linear.graphql import LinearGraphQLClient, summarize_error_body
from symphony.trackers.linear.queries import (
    CREATE_COMMENT_MUTATION,
    QUERY,
    QUERY_BY_IDS,
    STATE_LOOKUP_QUERY,
    UPDATE_STATE_MUTATION,
    VIEWER_QUERY,
)
from symphony.trackers.memory import MemoryTracker
from symphony.trackers.memory.tracker import build_tracker as build_memory_tracker

from .conftest import make_issue, make_snapshot, write_workflow_file


def make_settings(tmp_path: Path, *, tracker: dict[str, Any] | None = None):
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", tracker=tracker or {})
    return make_snapshot(workflow).settings


def test_tracker_base_build_validate_and_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_settings = make_settings(
        tmp_path, tracker={"kind": "memory", "api_key": None, "project_slug": None}
    )
    linear_settings = make_settings(tmp_path)

    monkeypatch.setattr(
        "symphony.trackers.memory.tracker.build_tracker",
        lambda settings, **kwargs: ("memory", settings, kwargs),
    )
    monkeypatch.setattr(
        "symphony.trackers.linear.client.build_tracker",
        lambda settings, **kwargs: ("linear", settings, kwargs),
    )
    assert cast(Any, build_tracker(memory_settings, sample=True))[0] == "memory"
    assert cast(Any, build_tracker(linear_settings, sample=True))[0] == "linear"

    with pytest.raises(ConfigValidationError, match="tracker.kind is required"):
        validate_tracker_settings(
            make_settings(
                tmp_path, tracker={"kind": None, "api_key": "t", "project_slug": "p"}
            )
        )
    validate_tracker_settings(memory_settings)
    with pytest.raises(ConfigValidationError, match="unsupported tracker kind"):
        validate_tracker_settings(
            make_settings(
                tmp_path, tracker={"kind": "jira", "api_key": "t", "project_slug": "p"}
            )
        )

    parse_calls: list[Any] = []
    validate_calls: list[Any] = []
    monkeypatch.setattr(
        "symphony.trackers.linear.config.validate_tracker_settings",
        lambda settings: validate_calls.append(settings),
    )
    validate_tracker_settings(linear_settings)
    assert validate_calls == [linear_settings]

    tracker = parse_tracker_settings({"tracker": {"kind": "memory"}})
    assert tracker.kind == "memory"
    parsed_linear_tracker = linear_settings.tracker

    monkeypatch.setattr(
        "symphony.trackers.linear.config.parse_tracker_settings",
        lambda config: parse_calls.append(config) or parsed_linear_tracker,
    )
    parse_tracker_settings({"tracker": {"kind": "linear"}})
    assert parse_calls


def test_linear_config_defaults_env_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "env-token")
    monkeypatch.setenv("LINEAR_ASSIGNEE", "user-1")
    tracker = parse_linear_tracker_settings({"tracker": {"kind": "linear"}})
    assert tracker.kind == "linear"
    assert tracker.api_key == "env-token"
    assert tracker.assignee == "user-1"
    assert supports_tracker_kind("linear") is True
    assert supports_tracker_kind("memory") is False

    monkeypatch.delenv("LINEAR_API_KEY")
    with pytest.raises(ConfigValidationError, match="LINEAR_API_KEY is required"):
        validate_linear_tracker_settings(
            make_settings(tmp_path, tracker={"api_key": None, "project_slug": "p"})
        )
    with pytest.raises(ConfigValidationError, match="tracker.project_slug is required"):
        validate_linear_tracker_settings(
            make_settings(tmp_path, tracker={"project_slug": None})
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
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        decode_linear_response({"data": {}}, None)


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
            if query == CREATE_COMMENT_MUTATION:
                return {"data": {"commentCreate": {"success": payload["body"] == "ok"}}}
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
    await client.create_comment("issue-1", "ok")
    with pytest.raises(TrackerClientError, match="comment_create_failed"):
        await client.create_comment("issue-1", "bad")
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

    missing_slug_settings = make_settings(tmp_path, tracker={"project_slug": None})
    missing_slug_client = LinearClient(
        missing_slug_settings, client_factory=cast(Any, lambda: FakeGraphQL())
    )
    with pytest.raises(TrackerClientError, match="missing_linear_project_slug"):
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
