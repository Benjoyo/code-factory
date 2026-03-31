from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_factory.coding_agents.codex.app_server.tool_response import (
    build_tool_response,
)
from code_factory.coding_agents.codex.tools import (
    DynamicToolExecutor,
    supported_tool_names,
    tool_specs,
)
from code_factory.coding_agents.codex.tools.issue_read import tracker_states
from code_factory.errors import TrackerClientError


class FakeTrackerOps:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def read_issue(self, issue: str, **kwargs: object) -> dict:
        self.calls.append(("read_issue", {"issue": issue, **kwargs}))
        return {
            "issue": {
                "identifier": issue,
                "title": "Example",
                "state": {"name": "In Progress"},
            }
        }

    async def read_issues(self, **kwargs: object) -> dict:
        self.calls.append(("read_issues", dict(kwargs)))
        return {
            "issues": [
                {
                    "identifier": "ENG-1",
                    "title": "Example",
                    "state": {"name": "Todo"},
                }
            ],
            "count": 1,
        }

    async def read_states(self, **kwargs: object) -> dict:
        self.calls.append(("read_states", dict(kwargs)))
        return {"states": [{"name": "Todo"}, {"name": "In Progress"}]}

    async def create_issue(self, **kwargs: object) -> dict:
        self.calls.append(("create_issue", dict(kwargs)))
        return {"issue_id": "issue-1", "created": True}

    async def update_issue(self, issue: str, **kwargs: object) -> dict:
        self.calls.append(("update_issue", {"issue": issue, **kwargs}))
        return {"issue_id": issue, "updated": True}

    async def create_comment(self, issue: str, body: str) -> dict:
        self.calls.append(("create_comment", {"issue": issue, "body": body}))
        return {"comment_id": "comment-1", "created": True}

    async def update_comment(self, comment_id: str, body: str) -> dict:
        self.calls.append(("update_comment", {"comment_id": comment_id, "body": body}))
        return {"comment_id": comment_id, "updated": True}

    async def link_pr(self, issue: str, url: str, title: str | None) -> dict:
        self.calls.append(("link_pr", {"issue": issue, "url": url, "title": title}))
        return {"issue_id": issue, "linked": True}

    async def upload_file(self, file_path: str) -> dict:
        self.calls.append(("upload_file", {"file_path": file_path}))
        return {"asset_url": "https://example.com/file.png"}


def test_tool_specs_are_registry_driven() -> None:
    assert supported_tool_names() == [
        "tracker_issue_get",
        "tracker_issue_search",
        "tracker_issue_create",
        "tracker_issue_update",
        "tracker_comment_create",
        "tracker_comment_update",
        "tracker_pr_link",
        "tracker_file_upload",
    ]
    specs = {item["name"]: item for item in tool_specs()}
    assert set(specs) == set(supported_tool_names())
    assert "required" not in specs["tracker_issue_get"]["inputSchema"]
    assert "required" not in specs["tracker_issue_search"]["inputSchema"]
    assert specs["tracker_issue_create"]["inputSchema"]["required"] == ["title"]
    assert "required" not in specs["tracker_issue_update"]["inputSchema"]
    assert specs["tracker_comment_create"]["inputSchema"]["required"] == ["body"]
    assert specs["tracker_comment_update"]["inputSchema"]["required"] == [
        "comment_id",
        "body",
    ]
    assert specs["tracker_pr_link"]["inputSchema"]["required"] == ["url"]
    assert specs["tracker_file_upload"]["inputSchema"]["required"] == ["file_path"]


@pytest.mark.asyncio
async def test_tracker_issue_get_defaults_to_current_issue() -> None:
    ops = FakeTrackerOps()
    executor = DynamicToolExecutor(ops, current_issue="ENG-1", current_project="proj-1")

    outcome = await executor.execute(
        "tracker_issue_get",
        {"include_comments": False, "include_attachments": False},
    )

    assert outcome.event == "tool_call_completed"
    assert outcome.success is True
    assert ops.calls == [
        (
            "read_issue",
            {
                "issue": "ENG-1",
                "include_description": True,
                "include_comments": False,
                "include_attachments": False,
                "include_relations": True,
            },
        )
    ]
    assert json.loads(build_tool_response(outcome)["contentItems"][0]["text"]) == {
        "issue": {
            "identifier": "ENG-1",
            "title": "Example",
            "state": {"name": "In Progress"},
        }
    }


@pytest.mark.asyncio
async def test_tracker_issue_search_is_current_project_scoped_and_lightweight() -> None:
    ops = FakeTrackerOps()
    executor = DynamicToolExecutor(ops, current_issue="ENG-1", current_project="proj-1")

    outcome = await executor.execute(
        "tracker_issue_search",
        {"query": "example", "state": "Todo", "limit": 5},
    )

    assert outcome.success is True
    assert ops.calls == [
        (
            "read_issues",
            {
                "project": "proj-1",
                "state": "Todo",
                "query": "example",
                "limit": 5,
                "include_description": False,
                "include_comments": False,
                "include_attachments": False,
                "include_relations": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_tracker_states_defaults_to_current_issue() -> None:
    ops = FakeTrackerOps()
    executor = DynamicToolExecutor(
        ops,
        current_issue="ENG-1",
        current_project="proj-1",
        tools=(tracker_states,),
    )

    outcome = await executor.execute("tracker_states", {})

    assert outcome.success is True
    assert ops.calls == [
        ("read_states", {"issue": "ENG-1", "team": None, "project": None})
    ]


@pytest.mark.asyncio
async def test_write_tools_default_current_context_and_forward_supported_fields() -> (
    None
):
    ops = FakeTrackerOps()
    executor = DynamicToolExecutor(ops, current_issue="ENG-1", current_project="proj-1")

    assert (
        await executor.execute("tracker_issue_create", {"title": "Follow-up"})
    ).success
    assert (
        await executor.execute(
            "tracker_issue_update",
            {"description": "Updated", "blocked_by": ["ENG-2"]},
        )
    ).success
    assert (await executor.execute("tracker_comment_create", {"body": "note"})).success
    assert (
        await executor.execute(
            "tracker_comment_update",
            {"comment_id": "comment-1", "body": "edited"},
        )
    ).success
    assert (
        await executor.execute(
            "tracker_pr_link",
            {"url": "https://example.com/pr/1", "title": "PR 1"},
        )
    ).success
    assert (
        await executor.execute(
            "tracker_file_upload",
            {"file_path": "artifacts/failure.png"},
        )
    ).success

    assert ops.calls == [
        (
            "create_issue",
            {
                "title": "Follow-up",
                "description": None,
                "project": "proj-1",
                "team": None,
                "state": None,
                "priority": None,
                "assignee": None,
                "labels": [],
                "blocked_by": [],
            },
        ),
        (
            "update_issue",
            {
                "issue": "ENG-1",
                "title": None,
                "description": "Updated",
                "project": None,
                "team": None,
                "state": None,
                "priority": None,
                "assignee": None,
                "labels": None,
                "blocked_by": ["ENG-2"],
            },
        ),
        ("create_comment", {"issue": "ENG-1", "body": "note"}),
        ("update_comment", {"comment_id": "comment-1", "body": "edited"}),
        (
            "link_pr",
            {
                "issue": "ENG-1",
                "url": "https://example.com/pr/1",
                "title": "PR 1",
            },
        ),
        ("upload_file", {"file_path": "artifacts/failure.png"}),
    ]


@pytest.mark.asyncio
async def test_missing_current_issue_or_project_is_reported() -> None:
    ops = FakeTrackerOps()
    issue_executor = DynamicToolExecutor(ops)
    issue_outcome = await issue_executor.execute("tracker_issue_get", {})
    assert issue_outcome.success is False
    assert issue_outcome.payload == {"error": {"message": "`issue` is required"}}

    project_executor = DynamicToolExecutor(ops, current_issue="ENG-1")
    project_outcome = await project_executor.execute("tracker_issue_search", {})
    assert project_outcome.success is False
    assert project_outcome.payload == {"error": {"message": "`project` is required"}}


@pytest.mark.asyncio
async def test_unsupported_tool_reports_supported_names() -> None:
    executor = DynamicToolExecutor(
        FakeTrackerOps(), current_issue="ENG-1", current_project="proj-1"
    )
    outcome = await executor.execute("tracker_read", {})

    assert outcome.event == "unsupported_tool_call"
    assert outcome.success is False
    assert outcome.payload["error"]["supportedTools"] == supported_tool_names()


@pytest.mark.asyncio
async def test_tracker_operation_errors_remain_structured() -> None:
    class FailingTrackerOps:
        async def read_issues(self, **_kwargs: object) -> dict:
            raise TrackerClientError(("linear_api_status", 503))

    executor = DynamicToolExecutor(
        FailingTrackerOps(), current_issue="ENG-1", current_project="proj-1"
    )
    outcome = await executor.execute("tracker_issue_search", {})

    assert outcome.event == "tool_call_completed"
    assert outcome.success is False
    assert outcome.payload == {
        "error": {
            "message": "Tracker request failed with HTTP 503.",
            "status": 503,
        }
    }


@pytest.mark.asyncio
async def test_all_tool_handlers_surface_tracker_failures() -> None:
    class FailingTrackerOps:
        async def read_issue(self, *args: object, **kwargs: object) -> dict:
            raise TrackerClientError(("tracker_operation_failed", "read failed"))

        async def read_states(self, *args: object, **kwargs: object) -> dict:
            raise TrackerClientError(("tracker_operation_failed", "states failed"))

        async def read_issues(self, *args: object, **kwargs: object) -> dict:
            raise TrackerClientError(("tracker_operation_failed", "search failed"))

        async def create_issue(self, **kwargs: object) -> dict:
            raise TrackerClientError(("tracker_operation_failed", "create failed"))

        async def update_issue(self, issue: str, **kwargs: object) -> dict:
            raise TrackerClientError(("tracker_operation_failed", "update failed"))

        async def create_comment(self, issue: str, body: str) -> dict:
            raise TrackerClientError(("tracker_operation_failed", "comment failed"))

        async def update_comment(self, comment_id: str, body: str) -> dict:
            raise TrackerClientError(
                ("tracker_operation_failed", "comment edit failed")
            )

        async def link_pr(self, issue: str, url: str, title: str | None) -> dict:
            raise TrackerClientError(("tracker_operation_failed", "link failed"))

        async def upload_file(self, file_path: str) -> dict:
            raise TrackerClientError(("tracker_operation_failed", "upload failed"))

    executor = DynamicToolExecutor(
        FailingTrackerOps(), current_issue="ENG-1", current_project="proj-1"
    )

    expectations = {
        "tracker_issue_get": ({}, "read failed"),
        "tracker_issue_search": ({}, "search failed"),
        "tracker_issue_create": ({"title": "Follow-up"}, "create failed"),
        "tracker_issue_update": ({"description": "Updated"}, "update failed"),
        "tracker_comment_create": ({"body": "note"}, "comment failed"),
        "tracker_comment_update": (
            {"comment_id": "comment-1", "body": "edited"},
            "comment edit failed",
        ),
        "tracker_pr_link": ({"url": "https://example.com/pr/1"}, "link failed"),
        "tracker_file_upload": (
            {"file_path": "artifacts/failure.png"},
            "upload failed",
        ),
    }
    for tool_name, (arguments, expected_message) in expectations.items():
        outcome = await executor.execute(tool_name, arguments)
        assert outcome.success is False
        assert outcome.payload == {"error": {"message": expected_message}}

    states_executor = DynamicToolExecutor(
        FailingTrackerOps(),
        current_issue="ENG-1",
        current_project="proj-1",
        tools=(tracker_states,),
    )
    states_outcome = await states_executor.execute("tracker_states", {})
    assert states_outcome.success is False
    assert states_outcome.payload == {"error": {"message": "states failed"}}


@pytest.mark.asyncio
async def test_invalid_tool_input_and_fail_factory_paths_are_covered() -> None:
    executor = DynamicToolExecutor(
        FakeTrackerOps(), current_issue="ENG-1", current_project="proj-1"
    )
    outcome = await executor.execute("tracker_comment_update", {"body": "missing id"})
    assert outcome.success is False
    assert outcome.payload == {
        "error": {"message": "tracker_comment_update: `comment_id` is required"}
    }
