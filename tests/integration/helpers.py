from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from .support import IntegrationHarness


def hook_script(
    log_path: Path,
    label: str,
    *,
    sleep_s: float | None = None,
    exit_status: int | None = None,
) -> str:
    steps: list[str] = []
    if sleep_s is not None:
        steps.append(f"sleep {sleep_s}")
    steps.append(
        f'printf \'%s:{label}\\n\' "$(basename "$PWD")" >> {shlex.quote(str(log_path))}'
    )
    if exit_status is not None:
        steps.append(f"exit {exit_status}")
    return "\n".join(steps)


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def issue_state(harness: IntegrationHarness, issue_id: str) -> str | None:
    issue = harness.tracker.issue(issue_id)
    return issue.state if issue is not None else None


async def wait_for_snapshot(
    harness: IntegrationHarness, predicate, *, timeout: float = 2.0
):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        snapshot = await harness.snapshot()
        value = predicate(snapshot)
        if value:
            return snapshot if value is True else value
        if loop.time() >= deadline:
            raise AssertionError("Timed out waiting for snapshot condition")
        await asyncio.sleep(0.01)


async def request_refresh_and_settle(
    harness: IntegrationHarness, *, delay_s: float = 0.08
) -> None:
    assert harness.actor is not None
    await harness.actor.request_refresh()
    await asyncio.sleep(delay_s)
