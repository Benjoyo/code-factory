"""Typed configuration models representing the workflow settings tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TrackerSettings:
    """Tracker integration metadata such as the endpoint, API key, and active states."""

    kind: str | None = None
    endpoint: str = ""
    api_key: str | None = None
    project_slug: str | None = None
    assignee: str | None = None
    active_states: tuple[str, ...] = ("Todo", "In Progress")
    terminal_states: tuple[str, ...] = (
        "Closed",
        "Cancelled",
        "Canceled",
        "Duplicate",
        "Done",
    )


@dataclass(frozen=True, slots=True)
class PollingSettings:
    """Polling interval metadata used by the workflow store."""

    interval_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class WorkspaceSettings:
    """Workspace root override for per-issue workspace creation."""

    root: str = ""


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Limits for concurrent agents, retries, and per-state throttles."""

    max_concurrent_agents: int = 10
    max_turns: int = 20
    max_retry_backoff_ms: int = 300_000
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CodingAgentSettings:
    """Controls how the coding agent process is started and sandboxed."""

    command: str = ""
    model: str | None = None
    reasoning_effort: str | None = None
    approval_policy: str | dict[str, Any] = field(
        default_factory=lambda: {
            "reject": {
                "sandbox_approval": True,
                "rules": True,
                "mcp_elicitations": True,
            }
        }
    )
    thread_sandbox: str = "workspace-write"
    turn_sandbox_policy: dict[str, Any] | None = None
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


@dataclass(frozen=True, slots=True)
class HooksSettings:
    """Paths for hooks invoked before and after workspace lifecycle events."""

    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60_000


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    """Settings that keep the dashboard and refresh cadence configurable."""

    dashboard_enabled: bool = True
    refresh_ms: int = 1_000
    render_interval_ms: int = 16


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """HTTP settings for exposing the observability API."""

    port: int | None = None
    host: str = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class Settings:
    """Root settings model consumed by the orchestrator snapshots."""

    tracker: TrackerSettings
    polling: PollingSettings
    workspace: WorkspaceSettings
    agent: AgentSettings
    coding_agent: CodingAgentSettings
    hooks: HooksSettings
    observability: ObservabilitySettings
    server: ServerSettings
