from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from code_factory.runtime.messages import WorkerExited
from code_factory.runtime.orchestration import OrchestratorActor, RunningEntry
from code_factory.runtime.worker import IssueWorker
from code_factory.trackers.memory import MemoryTracker

from ..conftest import make_issue, make_snapshot, write_workflow_file


def write_fake_agent(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def patch_issue_worker_workpad(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.hydrate_workspace_workpad", _noop
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.sync_workspace_workpad", _noop
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.prepare_workspace_repository", _noop
    )


@pytest.mark.asyncio
async def test_worker_completes_one_state_and_updates_tracker(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    trace_file = tmp_path / "codex.trace"
    workspace_root.mkdir()
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
        2) printf '%s\n' '{{"id":2,"result":{{"thread":{{"id":"thread-cont"}}}}}}' ;;
        3) printf '%s\n' '{{"id":3,"result":{{"turn":{{"id":"turn-cont-1"}}}}}}'
           printf '%s\n' '{{"method":"item/completed","params":{{"item":{{"type":"agentMessage","text":"{{\\"decision\\":\\"transition\\",\\"summary\\":\\"done\\",\\"next_state\\":\\"Done\\"}}"}}}}}}'
           printf '%s\n' '{{"method":"turn/completed","params":{{"turn":{{"status":"completed"}}}}}}' ;;
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
    issue = make_issue(id="issue-continue", identifier="MT-247")
    queue: asyncio.Queue = asyncio.Queue()

    class SequenceTracker(MemoryTracker):
        def __init__(self) -> None:
            super().__init__(
                [
                    make_issue(
                        id="issue-continue", identifier="MT-247", state="In Progress"
                    )
                ]
            )

    tracker = SequenceTracker()

    worker = IssueWorker(
        issue=issue,
        workflow_snapshot=snapshot,
        orchestrator_queue=queue,
        tracker=tracker,
    )

    await worker.run()

    updates = []
    while not queue.empty():
        updates.append(await queue.get())

    session_started = [
        update
        for update in updates
        if getattr(update, "update", {}).get("event") == "session_started"
    ]
    assert len(session_started) == 1
    assert {event.update["thread_id"] for event in session_started} == {"thread-cont"}
    exited = next(update for update in updates if isinstance(update, WorkerExited))
    assert exited.completed is True
    assert (await tracker.fetch_issue_states_by_ids(["issue-continue"]))[
        0
    ].state == "Done"

    trace = trace_file.read_text(encoding="utf-8")
    assert trace.count('"method": "thread/start"') == 1


@pytest.mark.asyncio
async def test_orchestrator_releases_claim_on_completed_worker_and_tracks_token_totals(
    tmp_path: Path,
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    snapshot = make_snapshot(workflow)
    actor = OrchestratorActor(
        snapshot, tracker_factory=lambda settings: MemoryTracker([])
    )

    issue = make_issue(id="issue-usage", identifier="MT-201")
    entry = RunningEntry(
        issue_id="issue-usage",
        identifier=issue.identifier,
        issue=issue,
        workspace_path="/tmp/workspaces/MT-201",
        worker=object(),
        started_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    actor.running["issue-usage"] = entry
    actor.claimed.add("issue-usage")

    actor._integrate_agent_update(
        "issue-usage",
        {
            "event": "notification",
            "token_usage": {"inputTokens": 12, "outputTokens": 4, "totalTokens": 16},
            "timestamp": datetime.now(UTC),
            "session_id": "thread-live-turn-live",
            "runtime_pid": "4242",
            "message_summary": "thread/tokenUsage/updated",
        },
    )

    actor._integrate_agent_update(
        "issue-usage",
        {
            "event": "notification",
            "message_summary": "token_count",
            "timestamp": datetime.now(UTC),
        },
    )

    await actor._handle_worker_exited(
        WorkerExited(
            issue_id="issue-usage",
            identifier="MT-201",
            workspace_path="/tmp/workspaces/MT-201",
            normal=True,
            completed=True,
        )
    )

    assert "issue-usage" not in actor.retry_entries
    assert "issue-usage" not in actor.claimed
    assert actor.agent_totals["input_tokens"] == 12
    assert actor.agent_totals["output_tokens"] == 4
    assert actor.agent_totals["total_tokens"] == 16
