"""Issue worker that orchestrates agent sessions inside per-issue workspaces."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...coding_agents import (
    CodingAgentRuntime,
    CodingAgentSession,
    build_coding_agent_runtime,
)
from ...issues import Issue, normalize_issue_state
from ...prompts import build_prompt
from ...trackers.base import Tracker, build_tracker
from ...workflow.models import WorkflowSnapshot
from ...workspace import WorkspaceManager
from ..messages import AgentWorkerUpdate, WorkerExited
from ..support import maybe_aclose
from .results import build_prompt_issue_data, persist_state_result

LOGGER = logging.getLogger(__name__)


class IssueWorker:
    """Wraps the agent session loop and hooks for a single issue."""

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
        self._agent_runtime: CodingAgentRuntime | None = None
        self._remove_workspace_after_run = False

    async def stop(self, _reason: str | None = None) -> None:
        self.stop_event.set()
        if self._session is not None:
            await asyncio.shield(self._session.stop())

    async def run(self) -> None:
        normal = False
        completed = False
        reason: str | None = None
        try:
            workspace = await self.workspace_manager.create_for_issue(self.issue)
            self.workspace_path = workspace.path
            await self.workspace_manager.run_before_run_hook(workspace.path, self.issue)
            if self.stop_event.is_set():
                normal = True
                reason = "stopped"
                return
            if self._agent_runtime is None:
                self._agent_runtime = build_coding_agent_runtime(
                    self.workflow_snapshot.settings_for_state(self.issue.state),
                    self.tracker,
                )
            session = await self._require_agent_runtime().start_session(workspace.path)
            self._session = session
            try:
                await self._run_state(session)
                normal = True
                completed = True
            finally:
                await asyncio.shield(session.stop())
                self._session = None
        except Exception as exc:
            normal = self.stop_event.is_set()
            reason = "stopped" if normal else repr(exc)
            if not normal:
                LOGGER.exception(
                    "Issue worker failed issue_id=%s identifier=%s workspace=%s",
                    self.issue.id or "n/a",
                    self.issue.identifier or "n/a",
                    self.workspace_path or "n/a",
                )
        finally:
            if self.workspace_path is not None:
                try:
                    await self.workspace_manager.run_after_run_hook(
                        self.workspace_path, self.issue
                    )
                except Exception:
                    LOGGER.exception(
                        "after_run hook cleanup failed issue_id=%s workspace=%s",
                        self.issue.id or "n/a",
                        self.workspace_path,
                    )
                if self._remove_workspace_after_run:
                    try:
                        await self.workspace_manager.remove(self.workspace_path)
                    except Exception:
                        LOGGER.exception(
                            "workspace removal failed issue_id=%s workspace=%s",
                            self.issue.id or "n/a",
                            self.workspace_path,
                        )
            await maybe_aclose(self.tracker)
            await self.queue.put(
                WorkerExited(
                    issue_id=self.issue.id or "",
                    identifier=self.issue.identifier,
                    workspace_path=self.workspace_path,
                    normal=normal,
                    completed=completed,
                    reason=reason,
                )
            )

    async def _run_state(self, session: CodingAgentSession) -> None:
        if self.stop_event.is_set():
            return
        current_issue = await self._refresh_issue_state(self.issue) or self.issue
        profile = self.workflow_snapshot.state_profile(current_issue.state)
        if profile is None or not profile.is_agent_run:
            raise RuntimeError(
                f"worker_requires_agent_run_state: {current_issue.state!r}"
            )
        if self.stop_event.is_set():
            return
        prompt = await self._state_prompt(current_issue)
        result = await self._require_agent_runtime().run_turn(
            session,
            prompt,
            current_issue,
            on_message=self._on_agent_message,
        )
        if self.stop_event.is_set():
            return
        target_state = self._target_state(
            current_issue, result.decision, result.next_state
        )
        if not profile.allows_next_state(target_state):
            raise RuntimeError(
                f"invalid_next_state: {current_issue.state!r} -> {target_state!r}"
            )
        await persist_state_result(
            self.tracker, current_issue, current_issue.state or "", result
        )
        if not current_issue.id:
            raise RuntimeError("missing_issue_id_for_state_transition")
        await self.tracker.update_issue_state(current_issue.id, target_state)
        self._remove_workspace_after_run = _is_terminal_state(
            self.workflow_snapshot, target_state
        )

    async def _state_prompt(self, issue: Issue) -> str:
        issue_data = await build_prompt_issue_data(self.tracker, issue)
        return build_prompt(
            issue,
            self.workflow_snapshot,
            attempt=self.attempt,
            issue_data=issue_data,
        )

    def _target_state(self, issue: Issue, decision: str, next_state: str | None) -> str:
        profile = self.workflow_snapshot.state_profile(issue.state)
        if profile is None:
            raise RuntimeError(f"missing_state_profile: {issue.state!r}")
        if decision == "transition":
            if next_state is None:
                raise RuntimeError("missing_next_state_for_transition")
            target_state = next_state
        elif decision == "blocked":
            target_state = next_state or profile.failure_state
            if target_state is None:
                raise RuntimeError("missing_failure_state_for_blocked_result")
        else:
            raise RuntimeError(f"unsupported_turn_decision: {decision!r}")
        if (
            issue.state is not None
            and target_state.strip().lower() == issue.state.strip().lower()
        ):
            raise RuntimeError("next_state_must_not_equal_current_state")
        return target_state

    async def _refresh_issue_state(self, issue: Issue) -> Issue | None:
        if not issue.id:
            return None
        issues = await self.tracker.fetch_issue_states_by_ids([issue.id])
        return issues[0] if issues else None

    async def _on_agent_message(self, message: dict[str, Any]) -> None:
        if self.issue.id:
            await self.queue.put(AgentWorkerUpdate(self.issue.id, message))

    def _require_agent_runtime(self) -> CodingAgentRuntime:
        assert self._agent_runtime is not None
        return self._agent_runtime


def _is_terminal_state(workflow_snapshot: WorkflowSnapshot, state_name: str) -> bool:
    normalized = normalize_issue_state(state_name)
    return normalized in {
        normalize_issue_state(state)
        for state in workflow_snapshot.settings.tracker.terminal_states
    }
