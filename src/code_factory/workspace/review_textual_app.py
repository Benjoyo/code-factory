"""Textual review UI for the single-target operator flow."""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Awaitable, Callable, Sequence

from textual.app import App, ComposeResult
from textual.message import Message
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Log,
    Static,
    TabbedContent,
    TabPane,
)

from .paths import safe_identifier
from .review_models import ReviewTarget, RunningReviewServer
from .review_observer import NullReviewObserver, ReviewObserver

ReviewSession = Callable[[ReviewObserver, asyncio.Event], Awaitable[None]]


class PrepareLine(Message):
    def __init__(self, label: str, stream_name: str, line: str) -> None:
        self.label = label
        self.stream_name = stream_name
        self.line = line
        super().__init__()


class ServerStarted(Message):
    def __init__(self, entry: RunningReviewServer) -> None:
        self.entry = entry
        super().__init__()


class ServerLine(Message):
    def __init__(self, server_name: str, stream_name: str, line: str) -> None:
        self.server_name = server_name
        self.stream_name = stream_name
        self.line = line
        super().__init__()


class WarningRaised(Message):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__()


class SessionObserver(NullReviewObserver):
    def __init__(self, app: ReviewTextualApp) -> None:
        self._app = app

    def on_prepare_line(self, label: str, stream_name: str, line: str) -> None:
        self._app.post_message(PrepareLine(label, stream_name, line))

    def on_server_started(self, entry: RunningReviewServer) -> None:
        self._app.post_message(ServerStarted(entry))

    def on_server_line(
        self, entry: RunningReviewServer, stream_name: str, line: str
    ) -> None:
        self._app.post_message(ServerLine(entry.launch.name, stream_name, line))

    def on_warning(self, message: str) -> None:
        self._app.post_message(WarningRaised(message))


class ReviewTextualApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }
    #overview {
        height: auto;
    }
    #overview-table {
        height: 1fr;
    }
    #browser-button {
        margin: 1 0;
        width: 24;
    }
    #status {
        height: auto;
        color: yellow;
    }
    """
    BINDINGS = [
        ("q", "request_close", "Quit"),
        ("b", "open_browser", "Open Browser"),
    ]

    def __init__(
        self,
        *,
        target: ReviewTarget,
        servers: Sequence,
        prepare_enabled: bool,
        run_session: ReviewSession,
    ) -> None:
        super().__init__()
        self._target = target
        self._servers = tuple(servers)
        self._prepare_enabled = prepare_enabled
        self._run_session = run_session
        self._stop_event = asyncio.Event()
        self._row_urls: list[str | None] = []
        self._selected_row = 0
        self._log_ids = {
            server.name: f"log-{safe_identifier(server.name)}"
            for server in self._servers
        }
        self.session_error: Exception | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield DataTable(id="overview-table", cursor_type="row")
                yield Button("Open Browser", id="browser-button", disabled=True)
                yield Static("", id="status")
            for server in self._servers:
                with TabPane(server.name, id=f"tab-{safe_identifier(server.name)}"):
                    yield Log(id=self._log_ids[server.name], auto_scroll=True)
            with TabPane("Prepare", id="prepare"):
                yield Log(id="prepare-log", auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#overview-table", DataTable)
        table.add_columns("Target", "Server", "PID", "Port", "URL", "Ref", "PR", "Path")
        table.focus()
        if not self._prepare_enabled:
            self.query_one("#prepare-log", Log).write_line(
                "No review.prepare command configured."
            )
        self.run_worker(self._run_session_worker(), group="review", exclusive=True)

    async def _run_session_worker(self) -> None:
        try:
            await self._run_session(SessionObserver(self), self._stop_event)
        except Exception as exc:
            self.session_error = exc
        finally:
            self.call_after_refresh(self.exit)

    async def action_request_close(self) -> None:
        self._stop_event.set()
        self.query_one("#status", Static).update("Stopping review session...")

    async def action_open_browser(self) -> None:
        url = self._selected_url()
        if url is None:
            return
        opened = await asyncio.to_thread(webbrowser.open, url)
        if not opened:
            self.query_one("#status", Static).update(
                f"Failed to open browser for {url}"
            )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browser-button":
            await self.action_open_browser()

    def on_server_started(self, message: ServerStarted) -> None:
        entry = message.entry
        self.query_one("#overview-table", DataTable).add_row(
            entry.target.target,
            entry.launch.name,
            str(entry.process.pid or ""),
            str(entry.launch.port or ""),
            entry.launch.url or "",
            entry.head_sha,
            entry.target.pr_url or "",
            entry.worktree,
        )
        self._row_urls.append(entry.launch.url)
        self._refresh_browser_button()

    def on_data_table_row_highlighted(self, message: DataTable.RowHighlighted) -> None:
        self._selected_row = message.cursor_row
        self._refresh_browser_button()

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        self._selected_row = message.cursor_row
        self._refresh_browser_button()

    def on_prepare_line(self, message: PrepareLine) -> None:
        self.query_one("#prepare-log", Log).write_line(
            f"[{message.label}:{message.stream_name}] {message.line}"
        )

    def on_server_line(self, message: ServerLine) -> None:
        self.query_one(f"#{self._log_ids[message.server_name]}", Log).write_line(
            f"[{message.stream_name}] {message.line}"
        )

    def on_warning_raised(self, message: WarningRaised) -> None:
        self.query_one("#status", Static).update(message.message)
        self.query_one("#prepare-log", Log).write_line(f"[warning] {message.message}")

    def _refresh_browser_button(self) -> None:
        self.query_one("#browser-button", Button).disabled = (
            self._selected_url() is None
        )

    def _selected_url(self) -> str | None:
        if not self._row_urls:
            return None
        row = self._selected_row
        if not isinstance(row, int) or row < 0 or row >= len(self._row_urls):
            return None
        return self._row_urls[row]
