from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rich.console import Console
from textual.widgets import Button

from code_factory.config.models import ReviewServerSettings, ReviewSettings
from code_factory.errors import ReviewError
from code_factory.workspace.review_models import ReviewTarget, RunningReviewServer
from code_factory.workspace.review_observer import NullReviewObserver
from code_factory.workspace.review_runner import _open_browser, _wait_for_exit
from code_factory.workspace.review_session import (
    ReviewUiUnavailableError,
    _interactive_review_supported,
    _isatty,
    run_review_session,
    run_review_textual_session,
)
from code_factory.workspace.review_textual_app import ReviewTextualApp

from .conftest import write_workflow_file


def _review_settings() -> ReviewSettings:
    return ReviewSettings(
        prepare="prepare",
        servers=(ReviewServerSettings(name="web", command="run web"),),
    )


@pytest.mark.asyncio
async def test_run_review_session_prefers_textual_in_interactive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        review={"servers": [{"name": "web", "command": "run web"}]},
    )
    target = ReviewTarget("main", "main", None, None, "origin/main")
    calls: list[str] = []

    monkeypatch.setattr(
        "code_factory.workspace.review_session.resolve_repo_root",
        lambda _workflow_path: asyncio.sleep(0, result="/repo"),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_session.resolve_review_target",
        lambda repo_root, settings, target_name: asyncio.sleep(0, result=target),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_session._interactive_review_supported",
        lambda *_args, **_kwargs: True,
    )

    async def fake_tui(runner, repo_root, resolved_target, review) -> None:
        calls.append(resolved_target.target)
        assert repo_root == "/repo"
        assert review.servers[0].name == "web"

    monkeypatch.setattr(
        "code_factory.workspace.review_session.run_review_textual_session", fake_tui
    )
    await run_review_session(str(workflow), "main", keep=True)
    assert calls == ["main"]


@pytest.mark.asyncio
async def test_run_review_session_falls_back_to_console_when_ui_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        review={"servers": [{"name": "web", "command": "run web"}]},
    )
    target = ReviewTarget("main", "main", None, None, "origin/main")
    fallback_calls: list[tuple[str, str]] = []

    class FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def run(
            self, resolved_target, servers, *, observer=None, stop_event=None
        ):
            fallback_calls.append((resolved_target.target, servers[0].name))

    monkeypatch.setattr(
        "code_factory.workspace.review_session.resolve_repo_root",
        lambda _workflow_path: asyncio.sleep(0, result="/repo"),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_session.resolve_review_target",
        lambda repo_root, settings, target_name: asyncio.sleep(0, result=target),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_session._interactive_review_supported",
        lambda *_args, **_kwargs: True,
    )

    async def fail_tui(runner, repo_root, resolved_target, review) -> None:
        raise ReviewUiUnavailableError("boom")

    monkeypatch.setattr(
        "code_factory.workspace.review_session.run_review_textual_session",
        fail_tui,
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_session.ReviewRunner", FakeRunner
    )

    await run_review_session(
        str(workflow),
        "main",
        keep=False,
        console=Console(record=True),
    )
    assert fallback_calls == [("main", "web")]


@pytest.mark.asyncio
async def test_run_review_textual_session_success_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ReviewTarget("ENG-1", "ticket", "ENG-1", 1, "sha")
    review = _review_settings()

    class FakeApp:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.session_error = None

        async def run_async(self) -> None:
            return None

    monkeypatch.setattr(
        "code_factory.workspace.review_textual_app.ReviewTextualApp", FakeApp
    )
    runner = cast(Any, SimpleNamespace(run=lambda *args, **kwargs: asyncio.sleep(0)))
    await run_review_textual_session(runner, "/repo", target, review)

    class FailingApp(FakeApp):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.session_error = ReviewError("session failed")

    monkeypatch.setattr(
        "code_factory.workspace.review_textual_app.ReviewTextualApp", FailingApp
    )
    with pytest.raises(ReviewError, match="session failed"):
        await run_review_textual_session(runner, "/repo", target, review)


@pytest.mark.asyncio
async def test_review_textual_app_handles_warnings_button_paths_and_worker_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ReviewTarget(
        "ENG-1",
        "ticket",
        "ENG-1",
        1,
        "sha",
        pr_number=12,
        pr_url="https://example/pr/12",
    )
    servers = (ReviewServerSettings(name="web", command="run web"),)

    async def failing_session(observer, stop_event: asyncio.Event) -> None:
        observer.on_warning("warned")
        raise RuntimeError("boom")

    app = ReviewTextualApp(
        repo_root="/repo",
        target=target,
        servers=servers,
        prepare_enabled=True,
        run_session=failing_session,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
    assert isinstance(app.session_error, RuntimeError)

    async def idle_session(observer, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    browser_app = ReviewTextualApp(
        repo_root="/repo",
        target=target,
        servers=servers,
        prepare_enabled=True,
        run_session=idle_session,
    )
    async with browser_app.run_test() as pilot:
        await pilot.pause()
        await browser_app.action_open_preview()
        status = browser_app.query_one("#status")
        assert "warned" not in str(status.render())

        browser_app._row_urls = ["http://127.0.0.1:3001"]
        browser_app.on_data_table_row_selected(cast(Any, SimpleNamespace(cursor_row=0)))
        monkeypatch.setattr(
            "code_factory.workspace.review_textual_app.webbrowser.open",
            lambda _url: True,
        )
        await browser_app.action_open_preview()
        monkeypatch.setattr(
            "code_factory.workspace.review_textual_app.webbrowser.open",
            lambda _url: False,
        )
        button = browser_app.query_one("#preview-button", Button)
        await browser_app.on_button_pressed(Button.Pressed(button))
        await pilot.pause()
        assert "Failed to open preview" in str(
            browser_app.query_one("#status").render()
        )
        await browser_app.on_button_pressed(Button.Pressed(Button("Other", id="other")))

        browser_app._selected_row = -1
        assert browser_app._selected_url() is None
        await pilot.press("q")


@pytest.mark.asyncio
async def test_review_runner_stop_event_and_browser_helper(monkeypatch) -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    running = [
        cast(
            Any,
            SimpleNamespace(
                process=SimpleNamespace(wait=lambda: asyncio.sleep(0, result=0)),
                target=SimpleNamespace(target="ENG-1"),
                launch=SimpleNamespace(name="web"),
            ),
        )
    ]
    await _wait_for_exit(running, stop_event=stop_event)

    monkeypatch.setattr(
        "webbrowser.open",
        lambda url: url == "http://127.0.0.1:3001",
    )
    assert _open_browser("http://127.0.0.1:3001") is True


def test_review_session_tty_and_null_observer_helpers() -> None:
    assert _isatty(SimpleNamespace(isatty=lambda: True)) is True
    assert _isatty(SimpleNamespace(isatty=lambda: False)) is False
    assert _isatty(object()) is False
    assert (
        _interactive_review_supported(
            SimpleNamespace(isatty=lambda: True),
            SimpleNamespace(isatty=lambda: False),
        )
        is False
    )

    observer = NullReviewObserver()
    observer.on_prepare_line("label", "stdout", "line")
    observer.on_server_started(cast(Any, object()))
    observer.on_servers_ready(())
    observer.on_server_line(cast(Any, object()), "stderr", "line")
    observer.on_warning("warn")
