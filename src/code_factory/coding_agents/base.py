from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from ..config.models import CodingAgentSettings, Settings
from ..issues import Issue
from ..trackers.base import Tracker

AgentMessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CodingAgentSession(Protocol):
    async def stop(self) -> None: ...


class CodingAgentRuntime(Protocol):
    async def start_session(self, workspace: str) -> CodingAgentSession: ...

    async def run_turn(
        self,
        session: CodingAgentSession,
        prompt: str,
        issue: Issue,
        *,
        on_message: AgentMessageHandler | None = None,
    ) -> dict[str, Any]: ...


def build_coding_agent_runtime(
    settings: Settings, tracker: Tracker
) -> CodingAgentRuntime:
    from .codex.runtime import build_coding_agent_runtime as build_runtime

    return build_runtime(settings, tracker)


def validate_coding_agent_settings(settings: Settings) -> None:
    from .codex.config import (
        validate_coding_agent_settings as validate_runtime_settings,
    )

    validate_runtime_settings(settings)


def parse_coding_agent_settings(config: Mapping[str, Any]) -> CodingAgentSettings:
    from .codex.config import parse_coding_agent_settings as parse_runtime_settings

    return parse_runtime_settings(config)
