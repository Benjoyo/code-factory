"""Workflow-derived dashboard helpers kept separate from render layout code."""

from __future__ import annotations

from typing import Any

from rich.text import Text

from .dashboard_format import clean_inline, int_value, pick


def project_url(project_slug: str | None) -> str | None:
    """Provide a tracker-specific URL linking to the configured Linear project."""

    return (
        f"https://linear.app/project/{project_slug}/issues"
        if isinstance(project_slug, str) and project_slug.strip()
        else None
    )


def dashboard_url(host: str, port: int | None) -> str | None:
    """Build the observability dashboard URL, defaulting to localhost for wildcards."""

    if not isinstance(port, int) or port <= 0:
        return None
    cleaned = host.strip() if isinstance(host, str) else ""
    if cleaned in {"", "0.0.0.0", "::", "[::]"}:
        cleaned = "127.0.0.1"
    elif ":" in cleaned and not (cleaned.startswith("[") and cleaned.endswith("]")):
        cleaned = f"[{cleaned}]"
    return f"http://{cleaned}:{port}/"


def max_agents(workflow: dict[str, Any], configured_max_agents: int | None) -> int:
    agent = workflow.get("agent")
    if isinstance(agent, dict):
        return int_value(agent.get("max_concurrent_agents")) or int_value(
            configured_max_agents
        )
    return int_value(configured_max_agents)


def project_link(
    workflow: dict[str, Any], configured_project_url: str | None
) -> str | None:
    tracker = workflow.get("tracker")
    if isinstance(tracker, dict):
        live_link = project_url(str(pick(tracker, "project_slug") or ""))
        if live_link is not None:
            return live_link
    return configured_project_url


def dashboard_link(
    workflow: dict[str, Any],
    configured_dashboard_url: str | None,
    port_override: int | None,
) -> str | None:
    server = workflow.get("server")
    if isinstance(server, dict):
        live_host = str(pick(server, "host") or "")
        live_port = (
            port_override if isinstance(port_override, int) else pick(server, "port")
        )
        link = dashboard_url(live_host, int_value(live_port))
        if link is not None:
            return link
    return configured_dashboard_url


def workflow_status_text(workflow: dict[str, Any]) -> Text:
    version = pick(workflow, "version")
    loaded_at = pick(workflow, "loaded_at")
    if version is None and loaded_at is None:
        return Text("n/a", style="dim")
    text = f"v{version}" if version is not None else "unknown"
    if isinstance(loaded_at, str) and loaded_at.strip():
        text = f"{text} loaded {clean_inline(loaded_at, 32)}"
    return Text(text, style="cyan")
