from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CodeFactoryError(Exception):
    """Base error for Code Factory runtime failures."""


@dataclass(slots=True)
class WorkflowLoadError(CodeFactoryError):
    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class ConfigValidationError(CodeFactoryError):
    message: str
    code: str = "invalid_workflow_config"

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class TrackerClientError(CodeFactoryError):
    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class WorkspaceError(CodeFactoryError):
    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)


@dataclass(slots=True)
class AppServerError(CodeFactoryError):
    reason: Any

    def __str__(self) -> str:
        return repr(self.reason)
