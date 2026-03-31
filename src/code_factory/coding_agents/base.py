from __future__ import annotations

"""High-level protocols that keep runtimes interchangeable for the orchestrator."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from ..config.models import CodingAgentSettings, Settings
from ..issues import Issue
from ..structured_results import StructuredTurnResult
from ..trackers.base import Tracker
from .review_models import ReviewOutput

AgentMessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CodingAgentSession(Protocol):
    async def stop(self) -> None: ...


class CodingAgentRuntime(Protocol):
    async def start_session(self, workspace: str) -> CodingAgentSession: ...

    async def steer(self, session: CodingAgentSession, message: str) -> str | None: ...

    async def run_turn(
        self,
        session: CodingAgentSession,
        prompt: str,
        issue: Issue,
        *,
        on_message: AgentMessageHandler | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> StructuredTurnResult: ...

    async def run_review(
        self,
        workspace: str,
        prompt: str,
        issue: Issue,
        *,
        on_message: AgentMessageHandler | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ReviewOutput: ...


def build_coding_agent_runtime(
    settings: Settings, tracker: Tracker | None = None
) -> CodingAgentRuntime:
    # Codex is the default runtime. Swap this import if another agent is added.
    from .codex.runtime import build_coding_agent_runtime as build_runtime

    return build_runtime(settings, tracker)


def validate_coding_agent_settings(settings: Settings) -> None:
    # Keep validation in sync with the Codex runtime for now.
    from .codex.config import (
        validate_coding_agent_settings as validate_runtime_settings,
    )

    validate_runtime_settings(settings)


def parse_coding_agent_settings(config: Mapping[str, Any]) -> CodingAgentSettings:
    from .codex.config import parse_coding_agent_settings as parse_runtime_settings

    return parse_runtime_settings(config)
