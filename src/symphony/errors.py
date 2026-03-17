from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class SymphonyError(Exception):
    """Base error for Symphony runtime failures."""


@dataclass(slots=True)
class WorkflowLoadError(SymphonyError):
    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class ConfigValidationError(SymphonyError):
    message: str
    code: str = "invalid_workflow_config"

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class TrackerClientError(SymphonyError):
    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class WorkspaceError(SymphonyError):
    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class AppServerError(SymphonyError):
    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)
