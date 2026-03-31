"""Console rendering helpers for the operator review flow."""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console
from rich.table import Table

from .review_models import RunningReviewServer
from .review_observer import NullReviewObserver


def print_review_summary(
    console: Console, running: Sequence[RunningReviewServer]
) -> None:
    table = Table(title="Code Factory Review")
    table.add_column("Target")
    table.add_column("Server")
    table.add_column("PID")
    table.add_column("Port")
    table.add_column("URL", overflow="fold")
    table.add_column("Ref")
    table.add_column("PR", overflow="fold")
    table.add_column("Path", overflow="fold")
    for entry in running:
        table.add_row(
            entry.target.target,
            entry.launch.name,
            str(entry.process.pid or ""),
            str(entry.launch.port or ""),
            entry.launch.url or "",
            entry.head_sha,
            entry.target.pr_url or "",
            entry.worktree,
        )
    console.print(table)


class ReviewConsoleObserver(NullReviewObserver):
    """Write shared review events to a Rich console."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def on_prepare_line(self, label: str, stream_name: str, line: str) -> None:
        self._print_prefixed(label, stream_name, line)

    def on_servers_ready(self, running: Sequence[RunningReviewServer]) -> None:
        print_review_summary(self._console, running)

    def on_server_line(
        self, entry: RunningReviewServer, stream_name: str, line: str
    ) -> None:
        self._print_prefixed(entry.launch.name, stream_name, line)

    def on_warning(self, message: str) -> None:
        self._console.print(f"[warn]{message}[/warn]")

    def _print_prefixed(self, label: str, stream_name: str, line: str) -> None:
        self._console.print(
            f"[{label}:{stream_name}] {line}",
            markup=False,
            highlight=False,
        )
