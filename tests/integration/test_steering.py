from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import ClientSession

from ..conftest import make_issue
from .helpers import wait_for_snapshot
from .support import IntegrationHarness, TurnPlan


@pytest.mark.asyncio
async def test_integration_observability_steers_active_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = TurnPlan(pause_until_stopped=True)
    issue = make_issue(id="issue-901", identifier="ENG-901", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        run_http_server=True,
        workflow_overrides={"tracker": {"terminal_states": ["Done", "Canceled"]}},
        plans_by_identifier={"ENG-901": [plan]},
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-901" in harness.controller.active_plans)

        base_url = f"http://127.0.0.1:{harness.http_port}"
        async with ClientSession() as session:
            response = await session.post(
                f"{base_url}/api/v1/ENG-901/steer",
                json={"message": "Focus on failing tests first."},
            )
            payload = await response.json()
            assert response.status == 202
            assert payload["issue_identifier"] == "ENG-901"
            assert payload["turn_id"].startswith("dummy-thread-")

            bad = await session.post(f"{base_url}/api/v1/ENG-901/steer", json={})
            assert bad.status == 400

        assert plan.steers == ["Focus on failing tests first."]

        harness.tracker.mutate_issue("issue-901", state="Canceled")
        await harness.refresh()
        await wait_for_snapshot(harness, lambda snapshot: not snapshot["running"])
