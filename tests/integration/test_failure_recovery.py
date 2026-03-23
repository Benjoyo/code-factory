from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_factory.runtime.messages import AgentWorkerUpdate, WorkerExited

from ..conftest import make_issue, make_snapshot, write_workflow_file
from .helpers import issue_state, request_refresh_and_settle, wait_for_snapshot
from .support import IntegrationHarness, TurnPlan, transition_result


@pytest.mark.asyncio
async def test_integration_worker_errors_retry_with_capped_backoff_and_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-501", identifier="ENG-501", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "agent": {"max_retry_backoff_ms": 70},
        },
        plans_by_identifier={
            "ENG-501": [
                TurnPlan(error=RuntimeError("boom-1")),
                TurnPlan(error=RuntimeError("boom-2")),
                TurnPlan(error=RuntimeError("boom-3")),
                TurnPlan(result=transition_result("Done")),
            ]
        },
    ) as harness:
        await harness.refresh()
        retry_entry = await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-501" and entry["attempt"] == 3
                ),
                None,
            ),
        )
        assert retry_entry["error"].startswith("agent exited:")
        assert 0 < retry_entry["due_in_ms"] <= 70

        await harness.wait_until(lambda: issue_state(harness, "issue-501") == "Done")
        assert len(harness.controller.prompt_log["ENG-501"]) == 4


@pytest.mark.asyncio
async def test_integration_stall_timeout_restarts_worker_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-601", identifier="ENG-601", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "codex": {"stall_timeout_ms": 40},
            "agent": {"max_retry_backoff_ms": 60},
        },
        plans_by_identifier={
            "ENG-601": [
                TurnPlan(pause_until_stopped=True),
                TurnPlan(result=transition_result("Done")),
            ]
        },
    ) as harness:
        await harness.refresh()
        stalled_retry = await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-601"
                    and "stalled" in (entry["error"] or "")
                ),
                None,
            ),
        )
        assert stalled_retry["attempt"] == 1

        await harness.wait_until(lambda: issue_state(harness, "issue-601") == "Done")


@pytest.mark.asyncio
async def test_integration_dispatch_refresh_failures_and_stale_revalidation_do_not_launch_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-1001", identifier="ENG-1001", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={"tracker": {"terminal_states": ["Done", "Canceled"]}},
        plans_by_identifier={"ENG-1001": [TurnPlan(result=transition_result("Done"))]},
    ) as harness:
        actor = harness.actor
        assert actor is not None

        invalid_workflow = write_workflow_file(
            tmp_path / "INVALID_WORKFLOW.md",
            tracker={
                "kind": None,
                "api_key": None,
                "project_slug": None,
                "terminal_states": ["Done", "Canceled"],
            },
            workspace={"root": str(tmp_path / "workspaces")},
            codex={"command": "dummy-agent"},
        )
        await actor.notify_workflow_updated(make_snapshot(invalid_workflow))
        await harness.wait_until(
            lambda: actor.workflow_snapshot.path == str(invalid_workflow)
        )

        before_fetches = harness.tracker.fetch_candidate_calls
        await request_refresh_and_settle(harness)
        assert harness.tracker.fetch_candidate_calls == before_fetches
        assert "ENG-1001" not in harness.controller.prompt_log

        await actor.notify_workflow_updated(make_snapshot(harness.workflow_path))
        await harness.wait_until(
            lambda: actor.workflow_snapshot.path == str(harness.workflow_path)
        )

        original_fetch_candidates = harness.tracker.fetch_candidate_issues

        async def fail_candidates() -> list:
            raise RuntimeError("candidate fetch failed")

        monkeypatch.setattr(harness.tracker, "fetch_candidate_issues", fail_candidates)
        await request_refresh_and_settle(harness)
        assert "ENG-1001" not in harness.controller.prompt_log

        monkeypatch.setattr(
            harness.tracker, "fetch_candidate_issues", original_fetch_candidates
        )
        original_fetch_states = harness.tracker.fetch_issue_states_by_ids

        async def stale_revalidation(issue_ids: list[str]):
            if issue_ids == ["issue-1001"]:
                return [replace(issue, state="Done")]
            return await original_fetch_states(issue_ids)

        monkeypatch.setattr(
            harness.tracker, "fetch_issue_states_by_ids", stale_revalidation
        )
        await harness.refresh()

        snapshot = await harness.snapshot()
        assert snapshot["running"] == []
        assert snapshot["retrying"] == []
        assert "ENG-1001" not in harness.controller.prompt_log


@pytest.mark.asyncio
async def test_integration_retry_poll_failure_then_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-1011", identifier="ENG-1011", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "agent": {"max_retry_backoff_ms": 60},
        },
        plans_by_identifier={
            "ENG-1011": [
                TurnPlan(error=RuntimeError("boom")),
                TurnPlan(result=transition_result("Done")),
            ]
        },
    ) as harness:
        await harness.refresh()
        await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-1011" and entry["attempt"] == 1
                ),
                None,
            ),
        )

        original_fetch_candidates = harness.tracker.fetch_candidate_issues
        calls = {"count": 0}

        async def fail_once():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("retry poll failed")
            return await original_fetch_candidates()

        monkeypatch.setattr(harness.tracker, "fetch_candidate_issues", fail_once)
        retry_entry = await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-1011"
                    and entry["attempt"] == 2
                    and "retry poll failed" in (entry["error"] or "")
                ),
                None,
            ),
        )
        assert retry_entry["error"].startswith("retry poll failed:")

        await harness.wait_until(lambda: issue_state(harness, "issue-1011") == "Done")
        assert len(harness.controller.prompt_log["ENG-1011"]) == 2


@pytest.mark.asyncio
async def test_integration_retry_releases_missing_issue_and_reschedules_when_slots_are_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_issue = make_issue(id="issue-1021", identifier="ENG-001", state="Todo")
    running_issue = make_issue(id="issue-1022", identifier="ENG-999", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[missing_issue, running_issue],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "agent": {"max_concurrent_agents": 1, "max_retry_backoff_ms": 60},
        },
        plans_by_identifier={
            "ENG-001": [
                TurnPlan(error=RuntimeError("boom")),
                TurnPlan(result=transition_result("Done")),
            ],
            "ENG-999": [TurnPlan(pause_until_stopped=True)],
        },
    ) as harness:
        await harness.refresh()
        await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-1021" and entry["attempt"] == 1
                ),
                None,
            ),
        )

        harness.tracker.remove_issue("issue-1021")
        await wait_for_snapshot(
            harness,
            lambda snapshot: (
                all(entry["issue_id"] != "issue-1021" for entry in snapshot["running"])
                and all(
                    entry["issue_id"] != "issue-1021" for entry in snapshot["retrying"]
                )
            ),
        )
        assert len(harness.controller.prompt_log["ENG-001"]) == 1

    retry_issue = make_issue(id="issue-1031", identifier="ENG-001", state="Todo")
    slot_holder = make_issue(id="issue-1032", identifier="ENG-999", state="Todo")
    slot_full_tmp = tmp_path / "slot-full"
    slot_full_tmp.mkdir()

    async with IntegrationHarness(
        tmp_path=slot_full_tmp,
        monkeypatch=monkeypatch,
        issues=[retry_issue, slot_holder],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "agent": {"max_concurrent_agents": 1, "max_retry_backoff_ms": 60},
        },
        plans_by_identifier={
            "ENG-001": [
                TurnPlan(error=RuntimeError("boom")),
                TurnPlan(result=transition_result("Done")),
            ],
            "ENG-999": [TurnPlan(pause_until_stopped=True)],
        },
    ) as harness:
        await harness.refresh()
        await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-1031" and entry["attempt"] == 1
                ),
                None,
            ),
        )

        await harness.refresh()
        await harness.wait_until(lambda: "ENG-999" in harness.controller.prompt_log)
        retry_entry = await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-1031"
                    and entry["attempt"] == 2
                    and entry["error"] == "no available orchestrator slots"
                ),
                None,
            ),
        )
        assert retry_entry["attempt"] == 2

        harness.tracker.mutate_issue("issue-1032", state="Canceled")
        await harness.refresh()
        await harness.wait_until(lambda: issue_state(harness, "issue-1031") == "Done")
        assert len(harness.controller.prompt_log["ENG-001"]) == 2


@pytest.mark.asyncio
async def test_integration_retry_terminal_cleanup_uses_state_refresh_when_candidates_hide_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-1041", identifier="ENG-1041", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={"tracker": {"terminal_states": ["Done", "Canceled"]}},
        plans_by_identifier={"ENG-1041": [TurnPlan(error=RuntimeError("boom"))]},
    ) as harness:
        await harness.refresh()
        await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-1041" and entry["attempt"] == 1
                ),
                None,
            ),
        )

        actor = harness.actor
        assert actor is not None
        retry_entry = actor.retry_entries["issue-1041"]
        actor.retry_entries["issue-1041"] = replace(retry_entry, workspace_path=None)
        harness.tracker.mutate_issue("issue-1041", state="Done")

        async def hide_candidates() -> list:
            return []

        monkeypatch.setattr(harness.tracker, "fetch_candidate_issues", hide_candidates)
        workspace = tmp_path / "workspaces" / "ENG-1041"
        await harness.wait_until(lambda: not workspace.exists())
        await wait_for_snapshot(
            harness,
            lambda snapshot: not snapshot["running"] and not snapshot["retrying"],
        )
        assert len(harness.controller.prompt_log["ENG-1041"]) == 1


@pytest.mark.asyncio
async def test_integration_reconciliation_refresh_failure_non_active_and_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue_one = make_issue(id="issue-1051", identifier="ENG-1051", state="Todo")
    issue_two = make_issue(id="issue-1052", identifier="ENG-1052", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue_one],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "states": {"Todo": {"prompt": "default"}},
            "codex": {"stall_timeout_ms": 0},
        },
        plans_by_identifier={
            "ENG-1051": [TurnPlan(pause_until_stopped=True)],
            "ENG-1052": [TurnPlan(pause_until_stopped=True)],
        },
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-1051" in harness.controller.prompt_log)
        workspace_one = tmp_path / "workspaces" / "ENG-1051"

        original_fetch_states = harness.tracker.fetch_issue_states_by_ids

        async def fail_running_refresh(issue_ids: list[str]):
            if issue_ids == ["issue-1051"]:
                raise RuntimeError("refresh failed")
            return await original_fetch_states(issue_ids)

        monkeypatch.setattr(
            harness.tracker, "fetch_issue_states_by_ids", fail_running_refresh
        )
        await request_refresh_and_settle(harness)
        snapshot = await harness.snapshot()
        assert len(snapshot["running"]) == 1
        assert workspace_one.exists()

        monkeypatch.setattr(
            harness.tracker, "fetch_issue_states_by_ids", original_fetch_states
        )
        harness.tracker.mutate_issue("issue-1051", state="Backlog")
        await harness.refresh()
        await wait_for_snapshot(harness, lambda snapshot: not snapshot["running"])
        assert workspace_one.exists()

        harness.tracker.upsert_issue(issue_two)
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-1052" in harness.controller.prompt_log)
        workspace_two = tmp_path / "workspaces" / "ENG-1052"

        async def hide_running_issue(issue_ids: list[str]):
            if issue_ids == ["issue-1052"]:
                return []
            return await original_fetch_states(issue_ids)

        monkeypatch.setattr(
            harness.tracker, "fetch_issue_states_by_ids", hide_running_issue
        )
        await harness.refresh()
        await wait_for_snapshot(harness, lambda snapshot: not snapshot["running"])
        assert workspace_two.exists()


@pytest.mark.asyncio
async def test_integration_cleanup_failures_and_unknown_runtime_messages_do_not_corrupt_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-1061", identifier="ENG-1061", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={"tracker": {"terminal_states": ["Done", "Canceled"]}},
        plans_by_identifier={"ENG-1061": [TurnPlan(pause_until_stopped=True)]},
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-1061" in harness.controller.prompt_log)
        actor = harness.actor
        assert actor is not None

        await actor.queue.put(
            AgentWorkerUpdate(
                "issue-1061",
                {"event": "session_started", "timestamp": datetime.now(UTC)},
            )
        )
        await asyncio.sleep(0.05)
        snapshot = await harness.snapshot()
        assert len(snapshot["running"]) == 1
        assert snapshot["running"][0]["issue_id"] == "issue-1061"

        await actor.queue.put(
            AgentWorkerUpdate(
                "missing-issue",
                {"event": "notification", "timestamp": datetime.now(UTC)},
            )
        )
        await actor.queue.put(WorkerExited("missing-issue", "ENG-X", None, True))
        await asyncio.sleep(0.05)
        snapshot = await harness.snapshot()
        assert len(snapshot["running"]) == 1
        assert snapshot["running"][0]["issue_id"] == "issue-1061"

        class FailingManager:
            async def remove(self, _workspace_path: str) -> None:
                raise RuntimeError("cleanup failed")

        monkeypatch.setattr(
            actor,
            "_workspace_manager_for_path",
            lambda workspace_path: FailingManager(),
        )
        workspace = tmp_path / "workspaces" / "ENG-1061"
        harness.tracker.mutate_issue("issue-1061", state="Canceled")
        await harness.refresh()
        await wait_for_snapshot(harness, lambda current: not current["running"])
        assert workspace.exists()
