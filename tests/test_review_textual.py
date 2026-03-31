from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.widgets import Button, DataTable, Log, TabPane

from code_factory.config.models import ReviewServerSettings
from code_factory.runtime.subprocess.process_tree import ProcessTree
from code_factory.workspace.review_models import ReviewTarget, RunningReviewServer
from code_factory.workspace.review_textual_app import ReviewTextualApp


def _entry(
    target: ReviewTarget,
    server: ReviewServerSettings,
    *,
    pid: int,
    url: str | None,
) -> RunningReviewServer:
    launch = SimpleNamespace(
        name=server.name,
        command=server.command,
        port=server.base_port,
        url=url,
        open_browser=bool(url),
    )
    return RunningReviewServer(
        target=target,
        launch=cast(Any, launch),
        worktree="/tmp/review/eng-1",
        process=ProcessTree(
            process=cast(Any, SimpleNamespace(pid=pid, stdout=None, stderr=None)),
            command=server.command,
            cwd="/tmp/review/eng-1",
        ),
        head_sha="abc123",
    )


@pytest.mark.asyncio
async def test_review_textual_app_renders_overview_logs_and_prepare_tab() -> None:
    target = ReviewTarget("ENG-1", "ticket", "ENG-1", 1, "sha", pr_url="https://pr")
    servers = (
        ReviewServerSettings(name="web", command="run web", base_port=3001),
        ReviewServerSettings(name="api", command="run api"),
    )

    async def fake_run_session(observer, stop_event: asyncio.Event) -> None:
        observer.on_prepare_line("prepare", "stdout", "installing")
        observer.on_server_started(
            _entry(target, servers[0], pid=101, url="http://127.0.0.1:3001")
        )
        observer.on_server_started(_entry(target, servers[1], pid=202, url=None))
        observer.on_server_line(
            _entry(target, servers[0], pid=101, url="http://127.0.0.1:3001"),
            "stdout",
            "web ready",
        )
        observer.on_server_line(
            _entry(target, servers[1], pid=202, url=None), "stderr", "api waiting"
        )
        await stop_event.wait()

    app = ReviewTextualApp(
        target=target,
        servers=servers,
        prepare_enabled=True,
        run_session=fake_run_session,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#overview-table", DataTable)
        button = app.query_one("#browser-button", Button)
        prepare_log = app.query_one("#prepare-log", Log)
        assert table.row_count == 2
        assert button.disabled is False
        assert any("installing" in line for line in prepare_log.lines)

        await pilot.press("down")
        await pilot.pause()
        assert button.disabled is True

        web_log = app.query_one("#log-web", Log)
        api_log = app.query_one("#log-api", Log)
        assert any("web ready" in line for line in web_log.lines)
        assert any("api waiting" in line for line in api_log.lines)

        panes = list(app.query(TabPane))
        assert panes[0].id == "overview"
        assert panes[-1].id == "prepare"

        await pilot.press("q")


@pytest.mark.asyncio
async def test_review_textual_app_shows_empty_prepare_state() -> None:
    target = ReviewTarget("main", "main", None, None, "origin/main")
    servers = (ReviewServerSettings(name="web", command="run web"),)

    async def fake_run_session(observer, stop_event: asyncio.Event) -> None:
        observer.on_server_started(_entry(target, servers[0], pid=101, url=None))
        await stop_event.wait()

    app = ReviewTextualApp(
        target=target,
        servers=servers,
        prepare_enabled=False,
        run_session=fake_run_session,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        prepare_log = app.query_one("#prepare-log", Log)
        assert any(
            "No review.prepare command configured." in line
            for line in prepare_log.lines
        )
        await pilot.press("q")
