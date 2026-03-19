"""Shared result and error types for Codex dynamic tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Transport-neutral tool outcome returned by individual tool handlers."""

    success: bool
    payload: Any

    @classmethod
    def ok(cls, payload: Any) -> ToolResult:
        return cls(success=True, payload=payload)

    @classmethod
    def fail(cls, payload: Any) -> ToolResult:
        return cls(success=False, payload=payload)


@dataclass(frozen=True, slots=True)
class ToolExecutionOutcome:
    """Transport-neutral outcome returned by the dynamic tool executor."""

    success: bool
    payload: Any
    event: str


class ToolError(Exception):
    """Base class for structured tool failures surfaced to the model."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload)
        self.payload = payload


class ToolInputError(ToolError):
    """Raised when tool arguments fail validation."""


class ToolExecutionError(ToolError):
    """Raised when tool execution fails after arguments were accepted."""
