from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

from rich import box
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .dashboard_format import (
    agents_text,
    clean_inline,
    format_count,
    format_runtime,
    int_value,
    mapping_list,
    next_refresh_text,
    pick,
    rate_limits_text,
    tokens_text,
)


@dataclass(frozen=True, slots=True)
class StatusDashboardContext:
    max_agents: int
    project_url: str | None
    dashboard_url: str | None


def render_status_dashboard(
    snapshot: dict[str, Any],
    context: StatusDashboardContext,
    *,
    throughput_tps: float,
    unavailable: bool = False,
    unavailable_detail: str | None = None,
) -> RenderableType:
    if unavailable:
        return _unavailable_panel(unavailable_detail)
    running = mapping_list(snapshot.get("running"))
    retrying = mapping_list(snapshot.get("retrying"))
    totals = snapshot.get("agent_totals")
    totals = totals if isinstance(totals, dict) else {}
    runtime_seconds = int_value(totals.get("seconds_running")) + sum(
        int_value(entry.get("runtime_seconds")) for entry in running
    )
    summary = Table.grid(expand=True)
    summary.add_column(style="bold white", ratio=1)
    summary.add_column(ratio=5)
    summary.add_row("Agents:", agents_text(len(running), context.max_agents))
    summary.add_row(
        "Throughput:", Text(f"{format_count(int(throughput_tps))} tps", style="cyan")
    )
    summary.add_row("Runtime:", Text(format_runtime(runtime_seconds), style="magenta"))
    summary.add_row("Tokens:", tokens_text(totals))
    summary.add_row("Rate Limits:", rate_limits_text(snapshot.get("rate_limits")))
    summary.add_row(
        "Project:",
        Text(
            context.project_url or "n/a", style="cyan" if context.project_url else "dim"
        ),
    )
    if context.dashboard_url is not None:
        summary.add_row("Dashboard:", Text(context.dashboard_url, style="cyan"))
    summary.add_row("Next refresh:", next_refresh_text(snapshot.get("polling")))
    return Panel(
        Group(
            summary,
            Rule("Running", style="bright_black"),
            _running_renderable(running),
            Rule("Backoff queue", style="bright_black"),
            _retry_renderable(retrying),
        ),
        box=box.ROUNDED,
        border_style="white",
        padding=(0, 1),
        title=Text("SYMPHONY STATUS", style="bold white"),
        title_align="left",
    )


def project_url(project_slug: str | None) -> str | None:
    return (
        f"https://linear.app/project/{project_slug}/issues"
        if isinstance(project_slug, str) and project_slug.strip()
        else None
    )


def dashboard_url(host: str, port: int | None) -> str | None:
    if not isinstance(port, int) or port <= 0:
        return None
    cleaned = host.strip() if isinstance(host, str) else ""
    if cleaned in {"", "0.0.0.0", "::", "[::]"}:
        cleaned = "127.0.0.1"
    elif ":" in cleaned and not (cleaned.startswith("[") and cleaned.endswith("]")):
        cleaned = f"[{cleaned}]"
    return f"http://{cleaned}:{port}/"


def _unavailable_panel(detail: str | None) -> RenderableType:
    body = Table.grid(expand=True)
    body.add_row(Text("Orchestrator snapshot unavailable", style="bold red"))
    if isinstance(detail, str) and detail.strip():
        body.add_row(Text(clean_inline(detail, 120), style="yellow"))
    body.add_row(Text("Waiting for the next successful refresh.", style="dim"))
    return Panel(
        body,
        box=box.ROUNDED,
        border_style="bright_black",
        padding=(0, 1),
        title=Text("SYMPHONY STATUS", style="bold white"),
        title_align="left",
    )


def _running_renderable(running: list[dict[str, Any]]) -> RenderableType:
    if not running:
        return Padding(Text("No active agents", style="dim"), (0, 1))
    table = Table(
        expand=True,
        box=box.SIMPLE_HEAVY,
        show_edge=False,
        header_style="bold bright_black",
        padding=(0, 1),
    )
    for column in (
        "",
        "ID",
        "STAGE",
        "PID",
        "AGE / TURN",
        "TOKENS",
        "SESSION",
        "EVENT",
    ):
        table.add_column(column, overflow="ellipsis", no_wrap=column != "EVENT")
    for entry in sorted(running, key=lambda item: str(item.get("identifier") or "")):
        style = _event_style(
            entry.get("last_agent_event"), stopping=bool(entry.get("stopping"))
        )
        table.add_row(
            Text("●", style=style),
            Text(clean_inline(entry.get("identifier") or "unknown", 8), style="cyan"),
            Text(clean_inline(entry.get("state") or "unknown", 14), style=style),
            Text(clean_inline(entry.get("runtime_pid") or "n/a", 8), style="yellow"),
            Text(_runtime_and_turns(entry), style="magenta"),
            Text(format_count(int_value(entry.get("total_tokens"))), style="yellow"),
            Text(_compact_session_id(entry.get("session_id")), style="cyan"),
            Text(_event_summary(entry), style=style),
        )
    return table


def _retry_renderable(retrying: list[dict[str, Any]]) -> RenderableType:
    if not retrying:
        return Padding(Text("No queued retries", style="dim"), (0, 1))
    lines = Table.grid(expand=True)
    for entry in sorted(retrying, key=lambda item: int_value(item.get("due_in_ms"))):
        line = Text.assemble(
            ("↻ ", "yellow"),
            (
                clean_inline(
                    entry.get("identifier") or entry.get("issue_id") or "unknown", 24
                ),
                "red",
            ),
            (f" attempt={int_value(entry.get('attempt'))}", "yellow"),
            (" in ", "dim"),
            (_retry_due_text(entry.get("due_in_ms")), "cyan"),
        )
        error = clean_inline(entry.get("error"), 96)
        if error:
            line.append(f" error={error}", style="dim")
        lines.add_row(line)
    return lines


def _runtime_and_turns(entry: dict[str, Any]) -> str:
    turns = int_value(entry.get("turn_count"))
    runtime = format_runtime(int_value(entry.get("runtime_seconds")))
    return f"{runtime} / {turns}" if turns > 0 else runtime


def _compact_session_id(session_id: Any) -> str:
    text = clean_inline(session_id, 64) or "n/a"
    return text if len(text) <= 10 else f"{text[:4]}...{text[-6:]}"


def _event_summary(entry: dict[str, Any]) -> str:
    message = pick(entry.get("last_agent_message"), "message")
    return (
        clean_inline(message or entry.get("last_agent_event") or "none", 64) or "none"
    )


def _event_style(event: Any, *, stopping: bool) -> str:
    text = str(event or "").lower()
    if stopping or "error" in text or "fail" in text:
        return "red"
    if "complete" in text:
        return "magenta"
    if "start" in text:
        return "green"
    if "token" in text:
        return "yellow"
    return "bright_blue"


def _retry_due_text(due_in_ms: Any) -> str:
    ms = int_value(due_in_ms)
    return f"{ms // 1000}.{ms % 1000:03d}s"
