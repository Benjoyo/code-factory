from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientSession

from code_factory.application import CodeFactoryService

from ..conftest import make_issue, write_workflow_file
from .helpers import wait_for_snapshot
from .support import IntegrationHarness, RecordingMemoryTracker, TurnPlan


@pytest.mark.asyncio
async def test_integration_observability_http_endpoints_and_method_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-901", identifier="ENG-901", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        run_http_server=True,
        workflow_overrides={"tracker": {"terminal_states": ["Done", "Canceled"]}},
        plans_by_identifier={"ENG-901": [TurnPlan(pause_until_stopped=True)]},
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-901" in harness.controller.prompt_log)

        base_url = f"http://127.0.0.1:{harness.http_port}"
        async with ClientSession() as session:
            state_response = await session.get(f"{base_url}/api/v1/state")
            state_payload = await state_response.json()
            assert state_response.status == 200
            assert state_payload["counts"] == {"running": 1, "retrying": 0}
            assert state_payload["workflow"]["agent"]["max_concurrent_agents"] == 10
            assert state_payload["workflow"]["tracker"]["kind"] == "memory"

            issue_response = await session.get(f"{base_url}/api/v1/ENG-901")
            issue_payload = await issue_response.json()
            assert issue_response.status == 200
            assert issue_payload["status"] == "running"

            refresh_response = await session.post(f"{base_url}/api/v1/refresh", json={})
            refresh_payload = await refresh_response.json()
            assert refresh_response.status == 202
            assert refresh_payload["queued"] is True

            bad_method = await session.get(f"{base_url}/api/v1/refresh")
            assert bad_method.status == 405

            bad_method = await session.post(f"{base_url}/api/v1/state", json={})
            assert bad_method.status == 405

            missing = await session.get(f"{base_url}/api/v1/missing")
            assert missing.status == 404

        harness.tracker.mutate_issue("issue-901", state="Canceled")
        await harness.refresh()
        await wait_for_snapshot(harness, lambda snapshot: not snapshot["running"])


@pytest.mark.asyncio
@pytest.mark.parametrize("port_value", [None, 0])
async def test_integration_service_run_forever_starts_and_stops_with_memory_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port_value: int | None
) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        tracker={"kind": "memory"},
        workspace={"root": str(tmp_path / "workspaces")},
        server={"port": port_value},
        codex={"command": "dummy-agent"},
    )
    shared_tracker = RecordingMemoryTracker([])
    service = CodeFactoryService(str(workflow))

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.actor.build_tracker",
        lambda settings: shared_tracker,
    )
    monkeypatch.setattr(
        "code_factory.application.service.configure_logging", lambda logs_root: None
    )

    def install_stop(stop_event: asyncio.Event) -> None:
        asyncio.get_running_loop().call_later(0.1, stop_event.set)

    monkeypatch.setattr(service, "_install_signal_handlers", install_stop)
    await service.run_forever()

    assert service.orchestrator is not None
    assert service.workflow_store is not None
