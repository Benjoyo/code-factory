from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def normalize_issue_state(state_name: str | None) -> str:
    """Normalizes tracker state names for policy checks and comparisons."""

    return state_name.strip().lower() if isinstance(state_name, str) else ""


@dataclass(frozen=True, slots=True)
class BlockerRef:
    """Minimal blocker record kept on issues without importing tracker models."""

    id: str | None = None
    identifier: str | None = None
    state: str | None = None


@dataclass(frozen=True, slots=True)
class Issue:
    """Tracker-agnostic issue snapshot used throughout orchestration code."""

    id: str | None = None
    identifier: str | None = None
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    state: str | None = None
    branch_name: str | None = None
    url: str | None = None
    assignee_id: str | None = None
    blocked_by: tuple[BlockerRef, ...] = ()
    labels: tuple[str, ...] = ()
    assigned_to_worker: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def label_names(self) -> list[str]:
        """Returns a mutable label list for APIs that expect sequence editing."""

        return list(self.labels)
