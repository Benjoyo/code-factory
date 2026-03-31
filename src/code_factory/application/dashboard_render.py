"""Rich-based rendering helpers for the live status dashboard layout."""

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

from .dashboard_diagnostics import DiagnosticEntry, render_diagnostics_panel
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
from .dashboard_workflow import (
    dashboard_link,
    max_agents,
    project_link,
    workflow_status_text,
)


@dataclass(frozen=True, slots=True)
class StatusDashboardContext:
    """Optional fallback values plus immutable service-level overrides."""

    max_agents: int | None = None
    project_url: str | None = None
    dashboard_url: str | None = None
    port_override: int | None = None


RUNNING_TABLE_COLUMNS: tuple[dict[str, Any], ...] = (
    {"header": "", "width": 1},
    {"header": "ID", "width": 8, "style": "cyan"},
    {"header": "STAGE", "width": 14},
    {"header": "PID", "width": 8, "style": "yellow", "justify": "right"},
    {"header": "AGE / TURN", "width": 12, "style": "magenta"},
    {"header": "TOKENS", "width": 10, "style": "yellow", "justify": "right"},
    {"header": "SESSION", "width": 13, "style": "cyan"},
    {"header": "EVENT", "min_width": 24, "ratio": 1},
)

RETRY_TABLE_COLUMNS: tuple[dict[str, Any], ...] = (
    {"header": "ID", "width": 12, "style": "red"},
    {"header": "ATTEMPT", "width": 7, "style": "yellow", "justify": "right"},
    {"header": "DUE IN", "width": 8, "style": "cyan", "justify": "right"},
    {"header": "ERROR", "min_width": 24, "ratio": 1, "style": "dim"},
)


def render_status_dashboard(
    snapshot: dict[str, Any],
    context: StatusDashboardContext,
    *,
    throughput_tps: float,
    recent_logs: tuple[DiagnosticEntry, ...] = (),
    unavailable: bool = False,
    unavailable_detail: str | None = None,
) -> RenderableType:
    if unavailable:
        status_panel = _unavailable_panel(unavailable_detail)
    else:
        running = mapping_list(snapshot.get("running"))
        retrying = mapping_list(snapshot.get("retrying"))
        raw_workflow = snapshot.get("workflow")
        workflow: dict[str, Any] = (
            raw_workflow if isinstance(raw_workflow, dict) else {}
        )
        totals = snapshot.get("agent_totals")
        totals = totals if isinstance(totals, dict) else {}
        configured_max_agents = context.max_agents
        configured_project_url = context.project_url
        configured_dashboard_url = context.dashboard_url
        max_agents_value = max_agents(workflow, configured_max_agents)
        project_link_value = project_link(workflow, configured_project_url)
        dashboard_link_value = dashboard_link(
            workflow,
            configured_dashboard_url,
            context.port_override,
        )
        runtime_seconds = int_value(totals.get("seconds_running")) + sum(
            int_value(entry.get("runtime_seconds")) for entry in running
        )
        summary = Table.grid(expand=True)
        summary.add_column(style="bold white", ratio=1)
        summary.add_column(ratio=5)
        summary.add_row("Agents:", agents_text(len(running), max_agents_value))
        summary.add_row(
            "Throughput:",
            Text(f"{format_count(int(throughput_tps))} tps", style="cyan"),
        )
        summary.add_row(
            "Runtime:", Text(format_runtime(runtime_seconds), style="magenta")
        )
        summary.add_row("Workflow:", workflow_status_text(workflow))
        summary.add_row("Tokens:", tokens_text(totals))
        summary.add_row("Rate Limits:", rate_limits_text(snapshot.get("rate_limits")))
        summary.add_row(
            "Project:",
            Text(
                project_link_value or "n/a",
                style="cyan" if project_link_value else "dim",
            ),
        )
        if dashboard_link_value is not None:
            summary.add_row("Dashboard:", Text(dashboard_link_value, style="cyan"))
        reload_error = clean_inline(pick(workflow, "reload_error"), 96)
        if reload_error:
            summary.add_row("Reload error:", Text(reload_error, style="yellow"))
        summary.add_row("Next refresh:", next_refresh_text(snapshot.get("polling")))
        status_panel = Panel(
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
            title=Text("CODE FACTORY STATUS", style="bold white"),
            title_align="left",
        )
    diagnostics_panel = render_diagnostics_panel(recent_logs)
    return (
        Group(status_panel, diagnostics_panel)
        if diagnostics_panel is not None
        else status_panel
    )


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
        title=Text("CODE FACTORY STATUS", style="bold white"),
        title_align="left",
    )


def _running_renderable(running: list[dict[str, Any]]) -> RenderableType:
    """Render the currently running agents table, keeping IDs and events aligned."""

    if not running:
        return Padding(Text("No active agents", style="dim"), (0, 1))
    table = Table(
        expand=True,
        box=box.SIMPLE_HEAVY,
        show_edge=False,
        header_style="bold bright_black",
        padding=(0, 1),
    )
    for spec in RUNNING_TABLE_COLUMNS:
        header = str(spec["header"])
        table.add_column(
            header,
            overflow="ellipsis",
            no_wrap=header != "EVENT",
            width=spec.get("width"),
            min_width=spec.get("min_width"),
            ratio=spec.get("ratio"),
            justify=spec.get("justify", "left"),
            style=spec.get("style"),
        )
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
    """List queued retries along with their due time and any latest errors."""

    if not retrying:
        return Padding(Text("No queued retries", style="dim"), (0, 1))
    lines = Table(
        expand=True,
        box=box.SIMPLE_HEAVY,
        show_edge=False,
        header_style="bold bright_black",
        padding=(0, 1),
    )
    for spec in RETRY_TABLE_COLUMNS:
        header = str(spec["header"])
        lines.add_column(
            header,
            overflow="ellipsis",
            no_wrap=header != "ERROR",
            width=spec.get("width"),
            min_width=spec.get("min_width"),
            ratio=spec.get("ratio"),
            justify=spec.get("justify", "left"),
            style=spec.get("style"),
        )
    for entry in sorted(retrying, key=lambda item: int_value(item.get("due_in_ms"))):
        error = clean_inline(entry.get("error"), 96)
        lines.add_row(
            Text(
                clean_inline(
                    entry.get("identifier") or entry.get("issue_id") or "unknown", 12
                ),
                style="red",
            ),
            Text(str(int_value(entry.get("attempt"))), style="yellow"),
            Text(_retry_due_text(entry.get("due_in_ms")), style="cyan"),
            Text(error or "none", style="dim"),
        )
    return lines


def _runtime_and_turns(entry: dict[str, Any]) -> str:
    """Show runtime + turn information with a compact fallback when no turns have happened."""

    turns = int_value(entry.get("turn_count"))
    runtime = format_runtime(int_value(entry.get("runtime_seconds")))
    return f"{runtime} / {turns}" if turns > 0 else runtime


def _compact_session_id(session_id: Any) -> str:
    """Shorten a long session identifier for display without losing uniqueness."""

    text = clean_inline(session_id, 64) or "n/a"
    return text if len(text) <= 10 else f"{text[:4]}...{text[-6:]}"


def _event_summary(entry: dict[str, Any]) -> str:
    """Pick the most recent agent event message for human-readable display."""

    message = pick(entry.get("last_agent_message"), "message")
    return (
        clean_inline(message or entry.get("last_agent_event") or "none", 64) or "none"
    )


def _event_style(event: Any, *, stopping: bool) -> str:
    """Choose a color that reflects the last agent event and whether it's stopping."""

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
    """Express the retry delay in a fixed second.millisecond string for clarity."""

    ms = int_value(due_in_ms)
    return f"{ms // 1000}.{ms % 1000:03d}s"
