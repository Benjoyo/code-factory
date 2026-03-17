from __future__ import annotations

import asyncio
from typing import Any

from ...coding_agents import (
    CodingAgentRuntime,
    CodingAgentSession,
    build_coding_agent_runtime,
)
from ...issues import Issue
from ...prompts import build_prompt, continuation_prompt
from ...trackers.base import Tracker, build_tracker
from ...workflow.models import WorkflowSnapshot
from ...workspace import WorkspaceManager
from ..messages import AgentWorkerUpdate, WorkerExited
from ..support import maybe_aclose
from .utils import tracker_state_is_active


class IssueWorker:
    def __init__(
        self,
        *,
        issue: Issue,
        workflow_snapshot: WorkflowSnapshot,
        orchestrator_queue: asyncio.Queue[Any],
        attempt: int | None = None,
        tracker: Tracker | None = None,
    ) -> None:
        self.issue = issue
        self.workflow_snapshot = workflow_snapshot
        self.attempt = attempt
        self.workspace_manager = WorkspaceManager(workflow_snapshot.settings)
        self.tracker: Tracker = tracker or build_tracker(workflow_snapshot.settings)
        self.queue = orchestrator_queue
        self.stop_event = asyncio.Event()
        self.workspace_path: str | None = None
        self._session: CodingAgentSession | None = None
        self._agent_runtime: CodingAgentRuntime = build_coding_agent_runtime(
            workflow_snapshot.settings, self.tracker
        )

    async def stop(self, _reason: str | None = None) -> None:
        self.stop_event.set()
        if self._session is not None:
            await asyncio.shield(self._session.stop())

    async def run(self) -> None:
        normal = False
        reason: str | None = None
        try:
            workspace = await self.workspace_manager.create_for_issue(self.issue)
            self.workspace_path = workspace.path
            await self.workspace_manager.run_before_run_hook(workspace.path, self.issue)
            if self.stop_event.is_set():
                normal = True
                reason = "stopped"
                return
            session = await self._agent_runtime.start_session(workspace.path)
            self._session = session
            try:
                await self._run_turns(session)
                normal = True
            finally:
                await asyncio.shield(session.stop())
                self._session = None
        except Exception as exc:
            normal = self.stop_event.is_set()
            reason = "stopped" if normal else repr(exc)
        finally:
            if self.workspace_path is not None:
                await self.workspace_manager.run_after_run_hook(
                    self.workspace_path, self.issue
                )
            await maybe_aclose(self.tracker)
            await self.queue.put(
                WorkerExited(
                    issue_id=self.issue.id or "",
                    identifier=self.issue.identifier,
                    workspace_path=self.workspace_path,
                    normal=normal,
                    reason=reason,
                )
            )

    async def _run_turns(self, session: CodingAgentSession) -> None:
        max_turns = self.workflow_snapshot.settings.agent.max_turns
        current_issue = self.issue
        for turn_number in range(1, max_turns + 1):
            if self.stop_event.is_set():
                return
            prompt = self._turn_prompt(current_issue, turn_number, max_turns)
            await self._agent_runtime.run_turn(
                session, prompt, current_issue, on_message=self._on_agent_message
            )
            if self.stop_event.is_set():
                return
            refreshed_issue = await self._refresh_issue_state(current_issue)
            if refreshed_issue is None or not tracker_state_is_active(
                self.workflow_snapshot.settings, refreshed_issue.state
            ):
                return
            current_issue = refreshed_issue

    def _turn_prompt(self, issue: Issue, turn_number: int, max_turns: int) -> str:
        if turn_number == 1:
            return build_prompt(issue, self.workflow_snapshot, attempt=self.attempt)
        return continuation_prompt(turn_number, max_turns)

    async def _refresh_issue_state(self, issue: Issue) -> Issue | None:
        if not issue.id:
            return None
        issues = await self.tracker.fetch_issue_states_by_ids([issue.id])
        return issues[0] if issues else None

    async def _on_agent_message(self, message: dict[str, Any]) -> None:
        if self.issue.id:
            await self.queue.put(AgentWorkerUpdate(self.issue.id, message))
