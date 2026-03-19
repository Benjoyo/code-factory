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
from code_factory.errors import TrackerClientError


def test_tool_specs_are_registry_driven() -> None:
    assert supported_tool_names() == ["linear_graphql", "sync_workpad"]
    assert tool_specs() == [
        {
            "name": "linear_graphql",
            "description": "Execute a raw GraphQL query or mutation against Linear using Code Factory's configured auth.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "GraphQL query or mutation document to execute against Linear.",
                    },
                    "variables": {
                        "type": "object",
                        "description": "Optional GraphQL variables object.",
                        "additionalProperties": True,
                    },
                },
            },
        },
        {
            "name": "sync_workpad",
            "description": "Create or update a workpad comment on a Linear issue. Reads the body from a local file to keep the conversation context small.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["issue_id", "file_path"],
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": 'Linear issue identifier (e.g. "ENG-123") or internal UUID.',
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Path to a local markdown file whose contents become the comment body.",
                    },
                    "comment_id": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Existing comment ID to update. Omit to create a new comment.",
                    },
                },
            },
        },
    ]


@pytest.mark.asyncio
async def test_linear_graphql_tool_accepts_raw_query_string() -> None:
    called: list[tuple[str, dict]] = []

    async def fake_linear(query: str, variables: dict) -> dict:
        called.append((query, variables))
        return {"data": {"viewer": {"id": "usr_123"}}}

    executor = DynamicToolExecutor(fake_linear)
    outcome = await executor.execute(
        "linear_graphql", "  query Viewer { viewer { id } }  "
    )

    assert outcome.event == "tool_call_completed"
    assert called == [("query Viewer { viewer { id } }", {})]
    assert outcome.success is True
    assert build_tool_response(outcome)["success"] is True
    assert json.loads(build_tool_response(outcome)["contentItems"][0]["text"]) == {
        "data": {"viewer": {"id": "usr_123"}}
    }


@pytest.mark.asyncio
async def test_sync_workpad_is_bounded_to_workspace(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_linear(query: str, variables: dict) -> dict:
        calls.append((query, variables))
        return {"data": {"commentCreate": {"success": True}}}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workpad = workspace / "workpad.md"
    workpad.write_text("hello\n", encoding="utf-8")

    executor = DynamicToolExecutor(fake_linear, allowed_roots=(str(workspace),))
    outcome = await executor.execute(
        "sync_workpad",
        {"issue_id": "ENG-1", "file_path": "workpad.md"},
    )

    assert outcome.event == "tool_call_completed"
    assert outcome.success is True
    assert calls and calls[0][1]["issueId"] == "ENG-1"

    outside = tmp_path / "outside.md"
    outside.write_text("forbidden\n", encoding="utf-8")
    outcome = await executor.execute(
        "sync_workpad",
        {"issue_id": "ENG-1", "file_path": str(outside)},
    )
    assert outcome.event == "tool_call_completed"
    assert outcome.success is False

    outcome = await executor.execute(
        "sync_workpad",
        {"issue_id": "ENG-1", "file_path": "workpad.md", "extra": True},
    )
    assert outcome.event == "tool_call_completed"
    assert outcome.success is False
    payload = outcome.payload
    assert payload == {"error": {"message": "sync_workpad: unexpected field: `extra`"}}


@pytest.mark.asyncio
async def test_unsupported_tool_reports_supported_names() -> None:
    async def fake_linear(query: str, variables: dict) -> dict:
        return {"data": {}}

    executor = DynamicToolExecutor(fake_linear)
    outcome = await executor.execute("not_real", {})

    assert outcome.event == "unsupported_tool_call"
    assert outcome.success is False
    payload = outcome.payload
    assert payload["error"]["supportedTools"] == ["linear_graphql", "sync_workpad"]


@pytest.mark.asyncio
async def test_linear_graphql_preserves_reference_error_shapes() -> None:
    async def status_error(query: str, variables: dict) -> dict:
        raise TrackerClientError(("linear_api_status", 503))

    executor = DynamicToolExecutor(status_error)
    outcome = await executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert outcome.event == "tool_call_completed"
    assert outcome.success is False
    payload = outcome.payload
    assert payload == {
        "error": {
            "message": "Linear GraphQL request failed with HTTP 503.",
            "status": 503,
        }
    }

    async def request_error(query: str, variables: dict) -> dict:
        raise TrackerClientError(("linear_api_request", "timeout"))

    executor = DynamicToolExecutor(request_error)
    outcome = await executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert outcome.event == "tool_call_completed"
    assert outcome.success is False
    payload = outcome.payload
    assert payload == {
        "error": {
            "message": "Linear GraphQL request failed before receiving a successful response.",
            "reason": "'timeout'",
        }
    }
