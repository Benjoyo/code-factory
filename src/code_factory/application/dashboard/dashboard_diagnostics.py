"""Dashboard diagnostics buffer and Rich rendering for recent warnings/errors."""

from __future__ import annotations

import logging
import traceback
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

from rich import box
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text


@dataclass(frozen=True, slots=True)
class DiagnosticEntry:
    """Operator-facing warning/error record shown below the live dashboard."""

    timestamp: str
    level: str
    logger_name: str
    message: str


class DashboardDiagnostics:
    """Thread-safe ring buffer of recent warning/error log entries."""

    def __init__(
        self,
        *,
        max_entries: int = 5,
        max_chars: int = 12_000,
        max_lines_per_entry: int = 16,
    ) -> None:
        self._entries: deque[DiagnosticEntry] = deque(maxlen=max_entries)
        self._max_chars = max_chars
        self._max_lines_per_entry = max_lines_per_entry
        self._lock = Lock()

    def append_record(self, record: logging.LogRecord) -> None:
        """Capture a log record as a multiline dashboard diagnostic entry."""

        entry = DiagnosticEntry(
            timestamp=datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            level=record.levelname,
            logger_name=record.name,
            message=_truncate_message(
                _truncate_lines(_format_record(record), self._max_lines_per_entry),
                self._max_chars,
            ),
        )
        with self._lock:
            self._entries.appendleft(entry)

    def entries(self) -> tuple[DiagnosticEntry, ...]:
        """Return a stable snapshot of the current diagnostics buffer."""

        with self._lock:
            return tuple(self._entries)


class DashboardDiagnosticsHandler(logging.Handler):
    """Logging handler that forwards warning/error records into the dashboard buffer."""

    def __init__(self, diagnostics: DashboardDiagnostics) -> None:
        super().__init__(level=logging.WARNING)
        self.diagnostics = diagnostics

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.diagnostics.append_record(record)
        except Exception:
            self.handleError(record)


def render_diagnostics_panel(
    entries: Sequence[DiagnosticEntry],
) -> RenderableType | None:
    """Render the recent diagnostics panel when warning/error entries are available."""

    if not entries:
        return None
    blocks: list[RenderableType] = []
    for index, entry in enumerate(entries):
        blocks.append(_entry_header(entry))
        blocks.append(Padding(Text(entry.message.rstrip(), style="white"), (0, 1)))
        if index < len(entries) - 1:
            blocks.append(Rule(style="bright_black"))
    return Panel(
        Group(*blocks),
        box=box.ROUNDED,
        border_style="bright_black",
        padding=(0, 1),
        title=Text("RECENT WARNINGS / ERRORS", style="bold white"),
        title_align="left",
    )


def _entry_header(entry: DiagnosticEntry) -> Text:
    style = "red" if entry.level in {"ERROR", "CRITICAL"} else "yellow"
    return Text.assemble(
        (entry.timestamp, "dim"),
        ("  ", "dim"),
        (entry.level, style),
        ("  ", "dim"),
        (entry.logger_name, "cyan"),
    )


def _format_record(record: logging.LogRecord) -> str:
    sections = [record.getMessage().rstrip()]
    if record.exc_info:
        sections.append("".join(traceback.format_exception(*record.exc_info)).rstrip())
    if record.stack_info:
        sections.append(str(record.stack_info).rstrip())
    return "\n\n".join(section for section in sections if section)


def _truncate_message(message: str, max_chars: int) -> str:
    if len(message) <= max_chars:
        return message
    return message[:max_chars].rstrip() + "\n\n... (truncated)"


def _truncate_lines(message: str, max_lines: int) -> str:
    lines = message.splitlines()
    if len(lines) <= max_lines:
        return message
    return "\n".join([*lines[:max_lines], "... (truncated)"])
