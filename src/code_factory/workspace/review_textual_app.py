"""Textual review UI for the single-target operator flow."""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Awaitable, Callable, Sequence

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
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
from .review_textual_composer import CommentCountsUpdated, ReviewCommentComposer

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
    #body {
        height: 1fr;
        padding: 0 1 1 1;
    }
    #review-tabs {
        height: 1fr;
    }
    TabPane {
        padding: 1 1 0 1;
    }
    #overview {
        height: auto;
    }
    #overview-table {
        height: 1fr;
    }
    #overview-actions {
        height: auto;
        margin-top: 1;
        align: left middle;
    }
    #preview-button,
    #pr-button {
        width: 18;
        height: 3;
    }
    #pr-button {
        margin-left: 1;
    }
    #submission-summary {
        width: 1fr;
        text-align: right;
        color: $text-muted;
    }
    #status {
        height: auto;
        color: yellow;
        margin-top: 1;
    }
    """
    BINDINGS = [
        ("q", "request_close", "Quit"),
        ("b", "open_preview", "Open Preview"),
        ("p", "open_pr", "Open PR"),
    ]

    def __init__(
        self,
        *,
        repo_root: str,
        target: ReviewTarget,
        servers: Sequence,
        prepare_enabled: bool,
        run_session: ReviewSession,
    ) -> None:
        super().__init__()
        self._repo_root = repo_root
        self._target = target
        self._servers = tuple(servers)
        self._prepare_enabled = prepare_enabled
        self._run_session = run_session
        self._stop_event = asyncio.Event()
        self._row_urls: list[str | None] = []
        self._selected_row = 0
        self._comment_enabled = (
            target.kind == "ticket"
            and target.pr_number is not None
            and target.pr_url is not None
        )
        self._log_ids = {
            server.name: f"log-{safe_identifier(server.name)}"
            for server in self._servers
        }
        self.session_error: Exception | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            with TabbedContent(initial="overview", id="review-tabs"):
                with TabPane("Overview", id="overview"):
                    yield DataTable(id="overview-table", cursor_type="row")
                    with Horizontal(id="overview-actions"):
                        yield Button("Open Preview", id="preview-button", disabled=True)
                        if self._target.pr_url is not None:
                            yield Button("Open PR", id="pr-button")
                            yield Static(
                                self._submission_summary_text(0, 0),
                                id="submission-summary",
                            )
                    yield Static("", id="status")
                for server in self._servers:
                    with TabPane(server.name, id=f"tab-{safe_identifier(server.name)}"):
                        yield Log(id=self._log_ids[server.name], auto_scroll=True)
                with TabPane("Prepare", id="prepare"):
                    yield Log(id="prepare-log", auto_scroll=True)
            if self._comment_enabled:
                yield ReviewCommentComposer(
                    repo_root=self._repo_root, target=self._target
                )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#overview-table", DataTable)
        status = self.query_one("#status", Static)
        table.add_columns("Target", "Server", "PID", "Port", "URL", "Ref", "PR", "Path")
        status.display = False
        if not self._prepare_enabled:
            self.query_one("#prepare-log", Log).write_line(
                "No review.prepare command configured."
            )
        if self._comment_enabled:
            self.query_one(ReviewCommentComposer).focus_input()
        else:
            table.focus()
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
        self._set_status("Stopping review session...")

    async def action_open_preview(self) -> None:
        url = self._selected_url()
        if url is None:
            return
        opened = await asyncio.to_thread(webbrowser.open, url)
        if not opened:
            self._set_status(f"Failed to open preview for {url}")

    async def action_open_pr(self) -> None:
        pr_url = self._target.pr_url
        if pr_url is None:
            return
        opened = await asyncio.to_thread(webbrowser.open, pr_url)
        if not opened:
            self._set_status(f"Failed to open pull request for {pr_url}")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "preview-button":
            await self.action_open_preview()
        if event.button.id == "pr-button":
            await self.action_open_pr()

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
        self._set_status(message.message)
        self.query_one("#prepare-log", Log).write_line(f"[warning] {message.message}")

    def on_comment_counts_updated(self, message: CommentCountsUpdated) -> None:
        if not self._comment_enabled:
            return
        self.query_one("#submission-summary", Static).update(
            self._submission_summary_text(message.bug_count, message.change_count)
        )

    def _set_status(self, message: str) -> None:
        status = self.query_one("#status", Static)
        status.update(message)
        status.display = bool(message)

    def _refresh_browser_button(self) -> None:
        self.query_one("#preview-button", Button).disabled = (
            self._selected_url() is None
        )

    def _selected_url(self) -> str | None:
        if not self._row_urls:
            return None
        row = self._selected_row
        if not isinstance(row, int) or row < 0 or row >= len(self._row_urls):
            return None
        return self._row_urls[row]

    def _submission_summary_text(self, bug_count: int, change_count: int) -> str:
        return (
            f"Bugs submitted: {bug_count}  "
            f"Changes submitted: {change_count}"
        )
