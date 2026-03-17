from __future__ import annotations

import json
from pathlib import Path

import pytest

from symphony.coding_agents.codex.tools import DynamicToolExecutor
from symphony.errors import TrackerClientError


@pytest.mark.asyncio
async def test_linear_graphql_tool_accepts_raw_query_string() -> None:
    called: list[tuple[str, dict]] = []

    async def fake_linear(query: str, variables: dict) -> dict:
        called.append((query, variables))
        return {"data": {"viewer": {"id": "usr_123"}}}

    executor = DynamicToolExecutor(fake_linear)
    result, event = await executor.execute(
        "linear_graphql", "  query Viewer { viewer { id } }  "
    )

    assert event == "tool_call_completed"
    assert called == [("query Viewer { viewer { id } }", {})]
    assert result["success"] is True
    assert json.loads(result["contentItems"][0]["text"]) == {
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
    result, event = await executor.execute(
        "sync_workpad",
        {"issue_id": "ENG-1", "file_path": "workpad.md"},
    )

    assert event == "tool_call_completed"
    assert result["success"] is True
    assert calls and calls[0][1]["issueId"] == "ENG-1"

    outside = tmp_path / "outside.md"
    outside.write_text("forbidden\n", encoding="utf-8")
    result, event = await executor.execute(
        "sync_workpad",
        {"issue_id": "ENG-1", "file_path": str(outside)},
    )
    assert event == "tool_call_completed"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_unsupported_tool_reports_supported_names() -> None:
    async def fake_linear(query: str, variables: dict) -> dict:
        return {"data": {}}

    executor = DynamicToolExecutor(fake_linear)
    result, event = await executor.execute("not_real", {})

    assert event == "unsupported_tool_call"
    assert result["success"] is False
    payload = json.loads(result["contentItems"][0]["text"])
    assert payload["error"]["supportedTools"] == ["linear_graphql", "sync_workpad"]


@pytest.mark.asyncio
async def test_linear_graphql_preserves_reference_error_shapes() -> None:
    async def status_error(query: str, variables: dict) -> dict:
        raise TrackerClientError(("linear_api_status", 503))

    executor = DynamicToolExecutor(status_error)
    result, event = await executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert event == "tool_call_completed"
    assert result["success"] is False
    payload = json.loads(result["contentItems"][0]["text"])
    assert payload == {
        "error": {
            "message": "Linear GraphQL request failed with HTTP 503.",
            "status": 503,
        }
    }

    async def request_error(query: str, variables: dict) -> dict:
        raise TrackerClientError(("linear_api_request", "timeout"))

    executor = DynamicToolExecutor(request_error)
    result, event = await executor.execute(
        "linear_graphql", {"query": "query Viewer { viewer { id } }"}
    )
    assert event == "tool_call_completed"
    assert result["success"] is False
    payload = json.loads(result["contentItems"][0]["text"])
    assert payload == {
        "error": {
            "message": "Linear GraphQL request failed before receiving a successful response.",
            "reason": "'timeout'",
        }
    }
