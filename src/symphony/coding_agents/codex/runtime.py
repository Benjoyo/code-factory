from __future__ import annotations

import inspect
from typing import Any

from ...config.models import Settings
from ...errors import TrackerClientError
from ...issues import Issue
from ...trackers.base import Tracker
from ..base import AgentMessageHandler, CodingAgentRuntime, CodingAgentSession
from .app_server import AppServerClient, AppServerSession
from .tools import DynamicToolExecutor


class CodexRuntime:
    def __init__(self, settings: Settings, tracker: Tracker) -> None:
        self._tracker = tracker
        self._client = AppServerClient(
            settings,
            dynamic_tool_factory=self._build_dynamic_tool_executor,
        )

    async def start_session(self, workspace: str) -> AppServerSession:
        return await self._client.start_session(workspace)

    async def run_turn(
        self,
        session: CodingAgentSession,
        prompt: str,
        issue: Issue,
        *,
        on_message: AgentMessageHandler | None = None,
    ) -> dict[str, Any]:
        if not isinstance(session, AppServerSession):
            raise TypeError(f"Unsupported session type: {type(session)!r}")
        return await self._client.run_turn(
            session, prompt, issue, on_message=on_message
        )

    def _build_dynamic_tool_executor(self, workspace: str) -> DynamicToolExecutor:
        graphql = getattr(self._tracker, "graphql", None)

        async def graphql_call(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if callable(graphql):
                result = graphql(query, variables)
                if inspect.isawaitable(result):
                    return await result
                if isinstance(result, dict):
                    return result
            raise TrackerClientError("missing_linear_api_token")

        return DynamicToolExecutor(graphql_call, allowed_roots=(workspace,))


def build_coding_agent_runtime(
    settings: Settings, tracker: Tracker
) -> CodingAgentRuntime:
    return CodexRuntime(settings, tracker)
