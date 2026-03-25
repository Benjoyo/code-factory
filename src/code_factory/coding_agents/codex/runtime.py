from __future__ import annotations

"""Codex runtime wiring that keeps the orchestrator agnostic of the App Server protocol."""

from typing import Any

from ...config.models import Settings
from ...issues import Issue
from ...structured_results import StructuredTurnResult
from ...trackers import build_tracker_ops
from ...trackers.base import Tracker
from ..base import AgentMessageHandler, CodingAgentRuntime, CodingAgentSession
from .app_server import AppServerClient, AppServerSession
from .tools import DynamicToolExecutor


class CodexRuntime:
    """Real runtime implementation that proxies work through App Server sessions."""

    def __init__(self, settings: Settings, tracker: Tracker | None = None) -> None:
        self._settings = settings
        self._tracker = tracker
        self._client = AppServerClient(
            settings.coding_agent,
            settings.workspace,
            dynamic_tool_factory=self._build_dynamic_tool_executor,
        )

    async def start_session(self, workspace: str) -> AppServerSession:
        return await self._client.start_session(workspace)

    async def steer(self, session: CodingAgentSession, message: str) -> str | None:
        if not isinstance(session, AppServerSession):
            raise TypeError(f"Unsupported session type: {type(session)!r}")
        return await self._client.steer(session, message)

    async def run_turn(
        self,
        session: CodingAgentSession,
        prompt: str,
        issue: Issue,
        *,
        on_message: AgentMessageHandler | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> StructuredTurnResult:
        if not isinstance(session, AppServerSession):
            raise TypeError(f"Unsupported session type: {type(session)!r}")
        return await self._client.run_turn(
            session,
            prompt,
            issue,
            on_message=on_message,
            output_schema=output_schema,
        )

    def _build_dynamic_tool_executor(
        self, workspace: str, issue: Issue
    ) -> DynamicToolExecutor:
        """Expose shared tracker operations to tools that need them."""
        ops = build_tracker_ops(self._settings, allowed_roots=(workspace,))
        return DynamicToolExecutor(
            ops,
            allowed_roots=(workspace,),
            current_issue=issue.identifier,
            current_project=self._settings.tracker.project_slug,
        )


def build_coding_agent_runtime(
    settings: Settings, tracker: Tracker | None = None
) -> CodingAgentRuntime:
    return CodexRuntime(settings, tracker)
