from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.widgets import Button, DataTable, Log, Static, TabPane, TextArea

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
    target = ReviewTarget(
        "ENG-1",
        "ticket",
        "ENG-1",
        1,
        "sha",
        pr_number=12,
        pr_url="https://pr",
    )
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
        repo_root="/repo",
        target=target,
        servers=servers,
        prepare_enabled=True,
        run_session=fake_run_session,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#overview-table", DataTable)
        preview_button = app.query_one("#preview-button", Button)
        pr_button = app.query_one("#pr-button", Button)
        summary = app.query_one("#submission-summary", Static)
        prepare_log = app.query_one("#prepare-log", Log)
        assert table.row_count == 2
        assert preview_button.disabled is False
        assert pr_button.label == "Open PR"
        assert str(summary.render()) == "Bugs submitted: 0  Changes submitted: 0"
        assert preview_button.region.y == pr_button.region.y == summary.region.y
        assert any("installing" in line for line in prepare_log.lines)

        app.on_data_table_row_selected(cast(Any, SimpleNamespace(cursor_row=1)))
        assert preview_button.disabled is True

        web_log = app.query_one("#log-web", Log)
        api_log = app.query_one("#log-api", Log)
        assert any("web ready" in line for line in web_log.lines)
        assert any("api waiting" in line for line in api_log.lines)

        panes = list(app.query(TabPane))
        assert panes[0].id == "overview"
        assert panes[-1].id == "prepare"
        assert app.query_one("#comment-input")
        assert app.query_one("#comment-kind", Button).label == "~ Change"

        await pilot.press("q")


@pytest.mark.asyncio
async def test_review_textual_ticket_composer_stays_visible() -> None:
    target = ReviewTarget(
        "ENG-1",
        "ticket",
        "ENG-1",
        1,
        "sha",
        pr_number=12,
        pr_url="https://pr",
    )
    servers = (ReviewServerSettings(name="web", command="run web"),)

    async def fake_run_session(observer, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    app = ReviewTextualApp(
        repo_root="/repo",
        target=target,
        servers=servers,
        prepare_enabled=True,
        run_session=fake_run_session,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        body = app.query_one("#body")
        composer = app.query_one("#comment-panel")
        text_area = app.query_one("#comment-input", TextArea)
        assert composer.region.y + composer.region.height <= (
            body.region.y + body.region.height
        )
        assert composer.region.x >= body.region.x
        assert text_area.region.height == 4
        assert (
            app.query_one("#comment-kind", Button).region.height
            == text_area.region.height
        )
        assert (
            app.query_one("#comment-submit", Button).region.height
            == text_area.region.height
        )
        await pilot.press("q")


@pytest.mark.asyncio
async def test_review_textual_app_shows_empty_prepare_state() -> None:
    target = ReviewTarget("main", "main", None, None, "origin/main")
    servers = (ReviewServerSettings(name="web", command="run web"),)

    async def fake_run_session(observer, stop_event: asyncio.Event) -> None:
        observer.on_server_started(_entry(target, servers[0], pid=101, url=None))
        await stop_event.wait()

    app = ReviewTextualApp(
        repo_root="/repo",
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
        assert list(app.query("#pr-button")) == []
        assert list(app.query("#comment-input")) == []
        await pilot.press("q")


@pytest.mark.asyncio
async def test_review_textual_ticket_composer_submits_and_counts_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ReviewTarget(
        "ENG-1",
        "ticket",
        "ENG-1",
        1,
        "sha",
        pr_number=12,
        pr_url="https://pr",
    )
    servers = (ReviewServerSettings(name="web", command="run web"),)
    submissions: list[tuple[str, int, str, str, str]] = []

    async def fake_run_session(observer, stop_event: asyncio.Event) -> None:
        observer.on_server_started(
            _entry(target, servers[0], pid=101, url="http://127.0.0.1:3001")
        )
        await stop_event.wait()

    async def fake_submit(
        repo_root: str, *, pr_number: int, pr_url: str, kind: str, body: str
    ):
        submissions.append((repo_root, pr_number, pr_url, kind, body))
        from code_factory.workspace.review_comments import SubmittedReviewComment

        return SubmittedReviewComment(kind=kind, body=body.strip(), pr_url=pr_url)

    monkeypatch.setattr(
        "code_factory.workspace.review_textual_composer.submit_review_comment",
        fake_submit,
    )
    app = ReviewTextualApp(
        repo_root="/repo",
        target=target,
        servers=servers,
        prepare_enabled=True,
        run_session=fake_run_session,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        text_area = app.query_one("#comment-input", TextArea)
        status = app.query_one("#status", Static)
        summary = app.query_one("#submission-summary", Static)
        text_area.load_text("first line")
        await pilot.press("tab")
        assert app.query_one("#comment-kind", Button).label == "! Bug"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert submissions == [("/repo", 12, "https://pr", "Bug", "first line")]
        assert text_area.text == ""
        assert status.display is False
        assert str(summary.render()) == "Bugs submitted: 1  Changes submitted: 0"
        await pilot.press("q")


@pytest.mark.asyncio
async def test_review_textual_open_pr_button_opens_ticket_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ReviewTarget(
        "ENG-1",
        "ticket",
        "ENG-1",
        1,
        "sha",
        pr_number=12,
        pr_url="https://pr",
    )
    servers = (ReviewServerSettings(name="web", command="run web"),)
    opened: list[str] = []

    async def fake_run_session(observer, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    monkeypatch.setattr(
        "code_factory.workspace.review_textual_app.webbrowser.open",
        lambda url: opened.append(url) or True,
    )
    app = ReviewTextualApp(
        repo_root="/repo",
        target=target,
        servers=servers,
        prepare_enabled=True,
        run_session=fake_run_session,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#pr-button")
        await pilot.pause()
        assert opened == ["https://pr"]
        await pilot.press("q")
