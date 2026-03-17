"""Workflow model types shared between the loader, store, and orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config.models import Settings


def utc_now() -> datetime:
    """Default factory so snapshots record load time in UTC."""

    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FileStamp:
    """Filesystem fingerprint used to detect workflow file changes."""

    mtime: int
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Parsed workflow document split into configuration and prompt template."""

    config: dict[str, Any]
    prompt_template: str


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    """Versioned, validated workflow payload consumed by the running service."""

    version: int
    path: str
    stamp: FileStamp
    definition: WorkflowDefinition
    settings: Settings
    loaded_at: datetime = field(default_factory=utc_now)

    @property
    def prompt_template(self) -> str:
        """Expose the prompt directly so callers do not reach into `definition`."""

        return self.definition.prompt_template


@dataclass(slots=True)
class WorkflowStoreState:
    """Mutable actor state that preserves the last known good workflow version."""

    path: str
    stamp: FileStamp
    workflow: WorkflowDefinition
    version: int
    last_reload_error: Any = None
