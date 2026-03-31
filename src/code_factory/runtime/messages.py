"""Structured messages shared between runtime workers and the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..workflow.models import WorkflowSnapshot


@dataclass(slots=True)
class WorkflowUpdated:
    """Broadcast when the workflow store publishes a new validated snapshot."""

    snapshot: WorkflowSnapshot


@dataclass(slots=True)
class WorkflowReloadError:
    """Delivered when a reload fails so the dashboard can report the trace."""

    error: Any


@dataclass(slots=True)
class AgentWorkerUpdate:
    """Carries worker-generated state for dashboard metrics and reconcilers."""

    issue_id: str
    update: dict[str, Any]


@dataclass(slots=True)
class WorkpadHydrated:
    """Signals that the worker prepared the local workpad for a running issue."""

    issue_id: str
    workspace_path: str
    workpad_path: str
    content_hash: str | None


@dataclass(slots=True)
class WorkerExited:
    """Terminal worker status emitted when a session exits or is stopped."""

    issue_id: str
    identifier: str | None
    workspace_path: str | None
    normal: bool
    reason: str | None = None
    completed: bool = False


@dataclass(slots=True)
class WorkerCleanupComplete:
    """Signals when cleanup of an issue workspace is finished."""

    issue_id: str
    workspace_path: str | None
    error: str | None = None


@dataclass(slots=True)
class SnapshotRequest:
    """Request/response envelope for the current orchestrator snapshot."""

    future: asyncio.Future[Any]


@dataclass(slots=True)
class RefreshRequest:
    """Request/response envelope that asks the orchestrator to poll immediately."""

    future: asyncio.Future[Any]


@dataclass(slots=True)
class SteerIssueRequest:
    """Request/response envelope that asks the orchestrator to steer a running issue."""

    future: asyncio.Future[Any]
    issue_identifier: str
    message: str


@dataclass(slots=True)
class Shutdown:
    """Request/response envelope used to stop the orchestrator loop."""

    future: asyncio.Future[Any]
