from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from code_factory.runtime.messages import WorkerExited
from code_factory.runtime.orchestration import OrchestratorActor, RunningEntry
from code_factory.runtime.worker import IssueWorker
from code_factory.trackers.memory import MemoryTracker

from .conftest import make_issue, make_snapshot, write_workflow_file


def write_fake_agent(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_worker_continues_on_same_thread_until_issue_leaves_active_state(
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
       printf '%s\n' '{{"method":"turn/completed"}}' ;;
    4) printf '%s\n' '{{"id":3,"result":{{"turn":{{"id":"turn-cont-2"}}}}}}'
       printf '%s\n' '{{"method":"turn/completed"}}' ;;
  esac
done
""",
    )

    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        workspace={"root": str(workspace_root)},
        codex={"command": f"{agent_runtime} app-server"},
        agent={"max_turns": 3},
    )
    snapshot = make_snapshot(workflow)
    issue = make_issue(id="issue-continue", identifier="MT-247")
    queue: asyncio.Queue = asyncio.Queue()

    class SequenceTracker(MemoryTracker):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        async def fetch_issue_states_by_ids(self, issue_ids: list[str]):
            self.calls += 1
            state = "In Progress" if self.calls == 1 else "Done"
            return [make_issue(id="issue-continue", identifier="MT-247", state=state)]

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
    assert len(session_started) == 2
    assert {event.update["thread_id"] for event in session_started} == {"thread-cont"}

    trace = trace_file.read_text(encoding="utf-8")
    assert trace.count('"method": "thread/start"') == 1


@pytest.mark.asyncio
async def test_orchestrator_schedules_continuation_retry_and_tracks_token_totals(
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
        )
    )

    retry = actor.retry_entries["issue-usage"]
    assert retry.attempt == 1
    assert actor.agent_totals["input_tokens"] == 12
    assert actor.agent_totals["output_tokens"] == 4
    assert actor.agent_totals["total_tokens"] == 16
