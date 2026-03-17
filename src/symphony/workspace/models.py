from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Workspace:
    path: str
    workspace_key: str
    created_now: bool
