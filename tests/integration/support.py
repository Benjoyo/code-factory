from __future__ import annotations

import asyncio
import contextlib
import itertools
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from code_factory.issues import Issue
from code_factory.observability.api.server import (
    ObservabilityHTTPServer,
    site_bound_port,
)
from code_factory.runtime.orchestration import OrchestratorActor
from code_factory.structured_results import StructuredTurnResult
from code_factory.trackers.memory import MemoryTracker
from code_factory.workflow.store import WorkflowStoreActor

from ..conftest import deep_merge, make_snapshot, write_workflow_file


class RecordingMemoryTracker(MemoryTracker):
    def __init__(self, issues: list[Issue] | None = None) -> None:
        super().__init__(issues)
        self.events: list[tuple[Any, ...]] = []
        self.fetch_candidate_calls = 0
        self.fetch_state_calls = 0

    @property
    def issues(self) -> list[Issue]:
        return list(self._issues)

    def issue(self, issue_id: str) -> Issue | None:
        return next((issue for issue in self._issues if issue.id == issue_id), None)

    def upsert_issue(self, issue: Issue) -> None:
        replaced = False
        updated: list[Issue] = []
        for current in self._issues:
            if current.id == issue.id:
                updated.append(issue)
                replaced = True
            else:
                updated.append(current)
        if not replaced:
            updated.append(issue)
        self._issues = updated

    def mutate_issue(self, issue_id: str, **changes: Any) -> None:
        issue = self.issue(issue_id)
        assert issue is not None, issue_id
        self.upsert_issue(replace(issue, **changes))

    def remove_issue(self, issue_id: str) -> None:
        self._issues = [issue for issue in self._issues if issue.id != issue_id]

    async def fetch_candidate_issues(self) -> list[Issue]:
        self.fetch_candidate_calls += 1
        self.events.append(
            ("fetch_candidate_issues", tuple(issue.id for issue in self._issues))
        )
        return await super().fetch_candidate_issues()

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        self.fetch_state_calls += 1
        self.events.append(("fetch_issue_states_by_ids", tuple(issue_ids)))
        return await super().fetch_issue_states_by_ids(issue_ids)

    async def fetch_issue_comments(self, issue_id: str):
        self.events.append(("fetch_issue_comments", issue_id))
        return await super().fetch_issue_comments(issue_id)

    async def create_comment(self, issue_id: str, body: str) -> None:
        self.events.append(("create_comment", issue_id, body))
        await super().create_comment(issue_id, body)

    async def update_comment(self, comment_id: str, body: str) -> None:
        self.events.append(("update_comment", comment_id, body))
        await super().update_comment(comment_id, body)

    async def update_issue_state(self, issue_id: str, state_name: str) -> None:
        self.events.append(("update_issue_state", issue_id, state_name))
        await super().update_issue_state(issue_id, state_name)
        updated: list[Issue] = []
        for issue in self._issues:
            blockers = tuple(
                replace(blocker, state=state_name)
                if blocker.id == issue_id
                else blocker
                for blocker in issue.blocked_by
            )
            updated.append(replace(issue, blocked_by=tuple(blockers)))
        self._issues = updated


@dataclass(slots=True)
class TurnPlan:
    sleep_ms: int = 0
    pause_until_stopped: bool = False
    error: BaseException | None = None
    message_summary: str | None = None
    token_usage: dict[str, int] | None = None
    rate_limits: dict[str, Any] | None = None
    messages: tuple[dict[str, Any], ...] = ()
    result: StructuredTurnResult | None = None
    steers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DummySession:
    workspace: str
    thread_id: str
    issue_identifier: str | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    turn_count: int = 0
    current_turn_id: str | None = None

    async def stop(self) -> None:
        self.stop_event.set()


class DummyAgentController:
    def __init__(
        self,
        plans_by_identifier: dict[str, list[TurnPlan]] | None = None,
        *,
        start_errors: dict[str, list[BaseException]] | None = None,
    ) -> None:
        self._plans = {
            identifier: list(plans)
            for identifier, plans in (plans_by_identifier or {}).items()
        }
        self._start_errors = {
            key: list(errors) for key, errors in (start_errors or {}).items()
        }
        self._thread_ids = itertools.count(1)
        self.prompt_log: dict[str, list[str]] = {}
        self.started_workspaces: list[str] = []
        self.active_plans: dict[str, TurnPlan] = {}

    def next_plan(self, identifier: str | None) -> TurnPlan:
        if not isinstance(identifier, str):
            return TurnPlan()
        queue = self._plans.setdefault(identifier, [])
        return queue.pop(0) if queue else TurnPlan()

    def take_start_error(self, workspace: str) -> BaseException | None:
        key = Path(workspace).name
        queue = self._start_errors.get(key)
        if not queue:
            return None
        error = queue.pop(0)
        if not queue:
            self._start_errors.pop(key, None)
        return error

    def record_prompt(self, identifier: str | None, prompt: str) -> None:
        if isinstance(identifier, str):
            self.prompt_log.setdefault(identifier, []).append(prompt)

    def new_thread_id(self) -> str:
        return f"dummy-thread-{next(self._thread_ids)}"

    def set_active_plan(self, identifier: str | None, plan: TurnPlan | None) -> None:
        if not isinstance(identifier, str):
            return
        if plan is None:
            self.active_plans.pop(identifier, None)
            return
        self.active_plans[identifier] = plan

    def active_plan(self, identifier: str | None) -> TurnPlan | None:
        if not isinstance(identifier, str):
            return None
        return self.active_plans.get(identifier)


class DummyRuntime:
    def __init__(
        self, controller: DummyAgentController, tracker: RecordingMemoryTracker
    ) -> None:
        self._controller = controller
        self._tracker = tracker

    async def start_session(self, workspace: str) -> DummySession:
        error = self._controller.take_start_error(workspace)
        if error is not None:
            raise error
        self._controller.started_workspaces.append(workspace)
        return DummySession(
            workspace=workspace, thread_id=self._controller.new_thread_id()
        )

    async def run_turn(
        self,
        session: DummySession,
        prompt: str,
        issue: Issue,
        *,
        on_message=None,
        output_schema=None,
    ) -> StructuredTurnResult:
        session.turn_count += 1
        self._controller.record_prompt(issue.identifier, prompt)
        plan = self._controller.next_plan(issue.identifier)
        session.issue_identifier = issue.identifier
        self._controller.set_active_plan(issue.identifier, plan)
        session_id = f"{session.thread_id}-turn-{session.turn_count}"
        session.current_turn_id = session_id
        updates = [
            {
                "event": "session_started",
                "timestamp": self._timestamp(),
                "thread_id": session.thread_id,
                "turn_id": session_id,
                "session_id": session_id,
                "runtime_pid": "dummy-runtime",
                "message_summary": "session_started",
            },
            {
                "event": "notification",
                "timestamp": self._timestamp(),
                "thread_id": session.thread_id,
                "turn_id": session_id,
                "session_id": session_id,
                "runtime_pid": "dummy-runtime",
                "message_summary": plan.message_summary or "dummy-turn",
                "token_usage": plan.token_usage or {},
                "rate_limits": plan.rate_limits,
            },
        ]
        updates.extend(plan.messages)
        if on_message is not None:
            for update in updates:
                await on_message(
                    update
                    if "timestamp" in update
                    else {**update, "timestamp": self._timestamp()}
                )
        if plan.pause_until_stopped:
            await session.stop_event.wait()
            session.current_turn_id = None
            self._controller.set_active_plan(issue.identifier, None)
            return StructuredTurnResult(decision="blocked", summary="stopped")
        if plan.sleep_ms > 0:
            await asyncio.sleep(plan.sleep_ms / 1000)
        if plan.error is not None:
            session.current_turn_id = None
            self._controller.set_active_plan(issue.identifier, None)
            raise plan.error
        session.current_turn_id = None
        self._controller.set_active_plan(issue.identifier, None)
        if plan.result is None:
            raise RuntimeError("missing_turn_plan_result")
        return plan.result

    async def steer(self, session: DummySession, message: str) -> str | None:
        if session.current_turn_id is None:
            raise RuntimeError("no_active_turn")
        plan = self._controller.active_plan(session.issue_identifier)
        if plan is None:
            raise RuntimeError("no_active_turn")
        plan.steers.append(message)
        return session.current_turn_id

    @staticmethod
    def _timestamp():
        from datetime import UTC, datetime

        return datetime.now(UTC)


def transition_result(
    next_state: str,
    *,
    summary: str = "completed",
    decision: str = "transition",
) -> StructuredTurnResult:
    return StructuredTurnResult(
        decision=decision,
        summary=summary,
        next_state=next_state,
    )


class IntegrationHarness:
    def __init__(
        self,
        *,
        tmp_path: Path,
        monkeypatch: Any,
        issues: list[Issue],
        workflow_overrides: dict[str, Any] | None = None,
        plans_by_identifier: dict[str, list[TurnPlan]] | None = None,
        start_errors: dict[str, list[BaseException]] | None = None,
        run_workflow_store: bool = False,
        run_http_server: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.workflow_overrides = workflow_overrides or {}
        self.tracker = RecordingMemoryTracker(issues)
        self.controller = DummyAgentController(
            plans_by_identifier, start_errors=start_errors
        )
        self.run_workflow_store = run_workflow_store
        self.run_http_server = run_http_server
        self.workflow_path = tmp_path / "WORKFLOW.md"
        self.actor: OrchestratorActor | None = None
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []
        self.workflow_store: WorkflowStoreActor | None = None
        self.http_runner: Any = None
        self.http_port: int | None = None

    async def __aenter__(self) -> IntegrationHarness:
        self.monkeypatch.setattr(OrchestratorActor, "FAILURE_RETRY_BASE_MS", 25)
        self.monkeypatch.setattr(
            OrchestratorActor, "POLL_TRANSITION_RENDER_DELAY_MS", 5
        )
        self.monkeypatch.setattr(
            "code_factory.runtime.worker.actor.build_coding_agent_runtime",
            lambda settings, tracker: DummyRuntime(self.controller, self.tracker),
        )
        workflow_config = deep_merge(
            {
                "tracker": {"kind": "memory"},
                "polling": {"interval_ms": 25},
                "workspace": {"root": str(self.tmp_path / "workspaces")},
                "codex": {"command": "dummy-agent"},
            },
            self.workflow_overrides,
        )
        workflow = write_workflow_file(
            self.workflow_path,
            **workflow_config,
        )
        snapshot = make_snapshot(workflow)
        self.actor = OrchestratorActor(
            snapshot, tracker_factory=lambda settings: self.tracker
        )
        await self.actor.startup_terminal_workspace_cleanup()
        self.tasks.append(asyncio.create_task(self.actor.run(self.stop_event)))
        if self.run_workflow_store:
            self.workflow_store = WorkflowStoreActor(
                str(self.workflow_path),
                on_snapshot=self.actor.notify_workflow_updated,
                on_error=self.actor.notify_workflow_reload_error,
                poll_interval_s=0.05,
            )
            self.tasks.append(
                asyncio.create_task(self.workflow_store.run(self.stop_event))
            )
        if self.run_http_server:
            server = ObservabilityHTTPServer(self.actor, host="127.0.0.1", port=0)
            self.http_runner = await server._start_runner()
            site = next(iter(self.http_runner.sites))
            self.http_port = site_bound_port(site)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.actor is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.actor.shutdown(), timeout=1)
        self.stop_event.set()
        for task in self.tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=1)
        if self.http_runner is not None:
            await self.http_runner.cleanup()

    async def refresh(self) -> dict[str, Any]:
        assert self.actor is not None
        before = self.tracker.fetch_candidate_calls
        response = await self.actor.request_refresh()
        await self.wait_until(lambda: self.tracker.fetch_candidate_calls > before)
        return response

    async def snapshot(self) -> dict[str, Any]:
        assert self.actor is not None
        return await self.actor.snapshot()

    async def wait_until(
        self,
        predicate,
        *,
        timeout: float = 2.0,
        interval: float = 0.01,
    ) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if loop.time() >= deadline:
                raise AssertionError("Timed out waiting for condition")
            await asyncio.sleep(interval)
