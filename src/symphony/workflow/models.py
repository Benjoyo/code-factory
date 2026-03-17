from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config.models import Settings


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FileStamp:
    mtime: int
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    config: dict[str, Any]
    prompt_template: str


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    version: int
    path: str
    stamp: FileStamp
    definition: WorkflowDefinition
    settings: Settings
    loaded_at: datetime = field(default_factory=utc_now)

    @property
    def prompt_template(self) -> str:
        return self.definition.prompt_template


@dataclass(slots=True)
class WorkflowStoreState:
    path: str
    stamp: FileStamp
    workflow: WorkflowDefinition
    version: int
    last_reload_error: Any = None
