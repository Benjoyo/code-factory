from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CodeFactoryError(Exception):
    """Base error for Code Factory runtime failures."""


@dataclass(slots=True)
class WorkflowLoadError(CodeFactoryError):
    """Wraps arbitrary loader failures so callers can preserve the root cause."""

    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class ConfigValidationError(CodeFactoryError):
    """Validation error formatted for operator-facing workflow feedback."""

    message: str
    code: str = "invalid_workflow_config"

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class TrackerClientError(CodeFactoryError):
    """Tracker integration failure that should not leak backend-specific types."""

    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class WorkspaceError(CodeFactoryError):
    """Workspace lifecycle failure raised by path, hook, or filesystem guards."""

    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class AppServerError(CodeFactoryError):
    """Codex app-server protocol or transport failure."""

    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class ControlRequestError(CodeFactoryError):
    """Operator-facing control-plane error with a stable HTTP status/code."""

    code: str
    message: str
    status: int

    def __str__(self) -> str:
        return self.message
