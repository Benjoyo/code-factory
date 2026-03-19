from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_factory.coding_agents.codex.app_server import AppServerClient
from code_factory.errors import AppServerError

from .conftest import make_issue, make_snapshot, write_workflow_file


def write_fake_agent(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_app_server_rejects_workspace_root_and_paths_outside_root(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    outside = tmp_path / "outside"
    workspace_root.mkdir()
    outside.mkdir()

    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md", workspace={"root": str(workspace_root)}
    )
    snapshot = make_snapshot(workflow)
    client = AppServerClient(
        snapshot.settings.coding_agent, snapshot.settings.workspace
    )
    issue = make_issue()

    with pytest.raises(AppServerError):
        await client.run(str(workspace_root), "guard", issue)

    with pytest.raises(AppServerError):
        await client.run(str(outside), "guard", issue)


@pytest.mark.asyncio
async def test_app_server_marks_input_required_as_failure(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "MT-88"
    workspace.mkdir(parents=True)
    agent_runtime = write_fake_agent(
        tmp_path / "fake-codex",
        """#!/bin/sh
count=0
while IFS= read -r line; do
  count=$((count + 1))
  case "$count" in
    1) printf '%s\n' '{"id":1,"result":{}}' ;;
    2) printf '%s\n' '{"id":2,"result":{"thread":{"id":"thread-88"}}}' ;;
    3) printf '%s\n' '{"id":3,"result":{"turn":{"id":"turn-88"}}}' ;;
    4) printf '%s\n' '{"method":"turn/input_required","params":{"requiresInput":true,"reason":"blocked"}}' ;;
  esac
done
""",
    )

    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        workspace={"root": str(workspace_root)},
        codex={"command": f"{agent_runtime} app-server"},
    )
    snapshot = make_snapshot(workflow)
    client = AppServerClient(
        snapshot.settings.coding_agent, snapshot.settings.workspace
    )

    with pytest.raises(AppServerError) as excinfo:
        await client.run(str(workspace), "Needs input", make_issue(identifier="MT-88"))

    assert "turn_input_required" in str(excinfo.value.reason)


@pytest.mark.asyncio
async def test_app_server_emits_tool_call_failed_for_supported_tool_failures(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "MT-90B"
    trace_file = tmp_path / "codex-tool-call-failed.trace"
    workspace.mkdir(parents=True)

    agent_runtime = write_fake_agent(
        tmp_path / "fake-codex",
        f"""#!/bin/sh
trace_file="{trace_file}"
count=0
while IFS= read -r line; do
  count=$((count + 1))
  printf 'JSON:%s\n' "$line" >> "$trace_file"
  case "$count" in
    1) printf '%s\n' '{{"id":1,"result":{{}}}}' ;;
    2) printf '%s\n' '{{"id":2,"result":{{"thread":{{"id":"thread-90b"}}}}}}' ;;
    3) printf '%s\n' '{{"id":3,"result":{{"turn":{{"id":"turn-90b"}}}}}}'
       printf '%s\n' '{{"id":103,"method":"item/tool/call","params":{{"tool":"linear_graphql","callId":"call-90b","threadId":"thread-90b","turnId":"turn-90b","arguments":{{"query":"query Viewer {{ viewer {{ id }} }}"}}}}}}' ;;
    4) printf '%s\n' '{{"method":"turn/completed"}}'
       exit 0 ;;
  esac
done
""",
    )

    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        workspace={"root": str(workspace_root)},
        codex={"command": f"{agent_runtime} app-server"},
    )
    snapshot = make_snapshot(workflow)
    client = AppServerClient(
        snapshot.settings.coding_agent, snapshot.settings.workspace
    )
    messages: list[dict] = []

    async def on_message(message: dict) -> None:
        messages.append(message)

    result = await client.run(
        str(workspace),
        "Handle failed tool calls",
        make_issue(identifier="MT-90B"),
        on_message=on_message,
        tool_executor=None,
    )

    assert result["result"] == "turn_completed"
    assert any(message["event"] == "tool_call_failed" for message in messages)
