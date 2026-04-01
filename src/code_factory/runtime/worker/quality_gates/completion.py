from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ....coding_agents.base import AgentMessageHandler, CodingAgentRuntime
from ....config.models import Settings
from ....issues import Issue
from ....structured_results import StructuredTurnResult
from ....workflow.models import WorkflowSnapshot, WorkflowStateProfile
from ....workspace.hooks import HookCommandResult, run_hook_command
from ...activity_phase import (
    EXECUTION_PHASE,
    QUALITY_GATES_PHASE,
    emit_activity_phase_update,
)
from .ai_review import run_ai_review_gate
from .pre_complete_feedback import (
    before_complete_exhausted_summary,
    before_complete_feedback_prompt,
    before_complete_hook_env,
    before_complete_issue_context,
    before_complete_update,
    emit_before_complete_update,
)
from .readiness import native_readiness_result

LOGGER = logging.getLogger(__name__)


async def run_before_complete_hook(
    settings: Settings,
    command: str,
    workspace_path: str,
    issue: Issue,
    result: StructuredTurnResult,
) -> HookCommandResult:
    return await run_hook_command(
        settings,
        command,
        workspace_path,
        before_complete_issue_context(issue),
        "before_complete",
        env=before_complete_hook_env(issue, result),
    )


async def run_pre_complete_turns(
    *,
    run_turn: Callable[[str], Awaitable[StructuredTurnResult]],
    settings: Settings,
    workspace_path: str | None,
    issue: Issue,
    profile: WorkflowStateProfile,
    queue: asyncio.Queue[object],
    issue_id: str | None,
    failure_state: str,
    initial_prompt: str,
    should_stop: Callable[[], bool],
    workflow_snapshot: WorkflowSnapshot,
    runtime: CodingAgentRuntime,
    on_message: AgentMessageHandler | None = None,
) -> StructuredTurnResult:
    """Run agent turns until deterministic and AI review gates accept or exhaust."""

    prompt = initial_prompt
    feedback_attempts = 0
    while True:
        await emit_activity_phase_update(
            queue,
            issue_id,
            event="execution_started",
            activity_phase=EXECUTION_PHASE,
        )
        result = await run_turn(prompt)
        if should_stop():
            return result
        if result.decision != "transition":
            return result
        if workspace_path is None:
            raise RuntimeError("missing_workspace_for_before_complete")

        await emit_activity_phase_update(
            queue,
            issue_id,
            event="quality_gates_started",
            activity_phase=QUALITY_GATES_PHASE,
        )
        native_result = await native_readiness_result(workspace_path, issue, profile)
        if native_result is not None:
            next_result = await _handle_gate_result(
                result=result,
                gate_result=native_result,
                queue=queue,
                issue_id=issue_id,
                event="before_complete",
                gate_source="native",
                gate_name="transition_readiness",
                feedback_attempts=feedback_attempts,
                max_feedback_loops=profile.hooks.before_complete_max_feedback_loops,
                failure_state=failure_state,
            )
            if next_result is not None:
                feedback_attempts, prompt, blocked = next_result
                if blocked is not None:
                    return blocked
                continue

        hook = profile.hooks.before_complete
        if hook is not None:
            await emit_activity_phase_update(
                queue,
                issue_id,
                event="quality_gates_started",
                activity_phase=QUALITY_GATES_PHASE,
            )
            hook_result = await run_before_complete_hook(
                settings,
                hook,
                workspace_path,
                issue,
                result,
            )
            if hook_result.status != 0:
                warned = await _handle_nonzero_hook_result(
                    result=result,
                    hook_result=hook_result,
                    issue=issue,
                    workspace_path=workspace_path,
                    queue=queue,
                    issue_id=issue_id,
                    feedback_attempts=feedback_attempts,
                    max_feedback_loops=profile.hooks.before_complete_max_feedback_loops,
                    failure_state=failure_state,
                )
                if warned is None:
                    return result
                feedback_attempts, prompt, blocked = warned
                if blocked is not None:
                    return blocked
                continue
            await emit_before_complete_update(
                queue,
                issue_id,
                "before_complete_passed",
                hook_result,
                gate_source="hook",
                gate_name="before_complete",
            )

        ai_review_result = await run_ai_review_gate(
            runtime=runtime,
            workflow_snapshot=workflow_snapshot,
            workspace_path=workspace_path,
            issue=issue,
            profile=profile,
            queue=queue,
            issue_id=issue_id,
            feedback_attempts=feedback_attempts,
            failure_state=failure_state,
            on_message=on_message,
        )
        if ai_review_result is None:
            return result
        feedback_attempts, prompt, blocked = ai_review_result
        if blocked is not None:
            return blocked


async def _handle_gate_result(
    *,
    result: StructuredTurnResult,
    gate_result: HookCommandResult,
    queue: asyncio.Queue[object],
    issue_id: str | None,
    event: str,
    gate_source: str,
    gate_name: str,
    feedback_attempts: int,
    max_feedback_loops: int,
    failure_state: str,
) -> tuple[int, str, StructuredTurnResult | None] | None:
    if gate_result.status == 0:
        await emit_before_complete_update(
            queue,
            issue_id,
            f"{event}_passed",
            gate_result,
            gate_source=gate_source,
            gate_name=gate_name,
        )
        return None
    await emit_before_complete_update(
        queue,
        issue_id,
        f"{event}_blocked",
        gate_result,
        gate_source=gate_source,
        gate_name=gate_name,
    )
    next_attempt = feedback_attempts + 1
    if next_attempt > max_feedback_loops:
        return (
            next_attempt,
            "",
            StructuredTurnResult(
                decision="blocked",
                summary=before_complete_exhausted_summary(
                    gate_result.stderr, max_feedback_loops
                ),
                next_state=failure_state,
            ),
        )
    return (
        next_attempt,
        before_complete_feedback_prompt(
            result,
            gate_result.stderr,
            next_attempt,
            max_feedback_loops,
            gate_source=gate_source,
            gate_name=gate_name,
        ),
        None,
    )


async def _handle_nonzero_hook_result(
    *,
    result: StructuredTurnResult,
    hook_result: HookCommandResult,
    issue: Issue,
    workspace_path: str,
    queue: asyncio.Queue[object],
    issue_id: str | None,
    feedback_attempts: int,
    max_feedback_loops: int,
    failure_state: str,
) -> tuple[int, str, StructuredTurnResult | None] | None:
    if hook_result.status != 2:
        LOGGER.warning(
            "before_complete hook failed but completion will continue issue_id=%s issue_identifier=%s workspace=%s status=%s stderr=%s",
            issue.id or "n/a",
            issue.identifier or "n/a",
            workspace_path,
            hook_result.status,
            hook_result.stderr.rstrip() or "<no stderr>",
        )
        await emit_before_complete_update(
            queue,
            issue_id,
            "before_complete_warned",
            hook_result,
            gate_source="hook",
            gate_name="before_complete",
        )
        return None
    return await _handle_gate_result(
        result=result,
        gate_result=hook_result,
        queue=queue,
        issue_id=issue_id,
        event="before_complete",
        gate_source="hook",
        gate_name="before_complete",
        feedback_attempts=feedback_attempts,
        max_feedback_loops=max_feedback_loops,
        failure_state=failure_state,
    )
