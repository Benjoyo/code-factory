from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..workflow.models import WorkflowSnapshot


@dataclass(slots=True)
class WorkflowUpdated:
    snapshot: WorkflowSnapshot


@dataclass(slots=True)
class WorkflowReloadError:
    error: Any


@dataclass(slots=True)
class AgentWorkerUpdate:
    issue_id: str
    update: dict[str, Any]


@dataclass(slots=True)
class WorkerExited:
    issue_id: str
    identifier: str | None
    workspace_path: str | None
    normal: bool
    reason: str | None = None


@dataclass(slots=True)
class WorkerCleanupComplete:
    issue_id: str
    workspace_path: str | None
    error: str | None = None


@dataclass(slots=True)
class SnapshotRequest:
    future: asyncio.Future[Any]


@dataclass(slots=True)
class RefreshRequest:
    future: asyncio.Future[Any]


@dataclass(slots=True)
class Shutdown:
    future: asyncio.Future[Any]
