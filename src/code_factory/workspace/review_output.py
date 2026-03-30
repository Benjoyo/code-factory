"""Console rendering helpers for the operator review flow."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .review_models import RunningReviewServer


def print_review_summary(console: Console, running: list[RunningReviewServer]) -> None:
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


def emit_prefixed_output(
    console: Console, label: str, stdout: str, stderr: str
) -> None:
    for stream_name, text in (("stdout", stdout), ("stderr", stderr)):
        for line in text.splitlines():
            console.print(
                f"[{label}:{stream_name}] {line}",
                markup=False,
                highlight=False,
            )
