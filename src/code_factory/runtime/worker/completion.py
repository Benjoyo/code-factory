from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ...config.models import Settings
from ...issues import Issue
from ...structured_results import StructuredTurnResult
from ...workflow.models import WorkflowStateProfile
from ...workspace.hooks import HookCommandResult, run_hook_command
from ..messages import AgentWorkerUpdate
from .readiness import native_readiness_result

BEFORE_COMPLETE_STDERR_LIMIT = 12_000
LOGGER = logging.getLogger(__name__)


def before_complete_feedback_prompt(
    result: StructuredTurnResult,
    stderr: str,
    attempt: int,
    max_attempts: int,
    *,
    gate_source: str = "hook",
    gate_name: str = "before_complete",
) -> str:
    feedback = stderr.strip() or (
        f"The {gate_name} {gate_source} gate exited with status 2 without any stderr output."
    )
    if len(feedback) > BEFORE_COMPLETE_STDERR_LIMIT:
        feedback = (
            f"{feedback[:BEFORE_COMPLETE_STDERR_LIMIT]}\n\n"
            "[truncated: before_complete stderr exceeded 12000 characters]"
        )
    gate_label = (
        "`before_complete` quality gate"
        if gate_source == "hook" and gate_name == "before_complete"
        else f"`{gate_name}` {gate_source} gate"
    )
    return (
        f"A {gate_label} blocked completion for this workflow state.\n"
        f"Feedback attempt {attempt} of {max_attempts}.\n"
        "Re-run the necessary validation, fix the reported problems, and then emit the required structured result again.\n"
        f"Previously proposed next state: {result.next_state or '<none>'}\n\n"
        "Gate stderr:\n"
        f"```text\n{feedback}\n```"
    )


def before_complete_hook_env(
    issue: Issue, result: StructuredTurnResult
) -> dict[str, str | None]:
    return {
        "CF_ISSUE_STATE": issue.state,
        "CF_RESULT_DECISION": result.decision,
        "CF_RESULT_NEXT_STATE": result.next_state,
    }


def before_complete_issue_context(issue: Issue) -> dict[str, str | None]:
    return {
        "issue_id": issue.id,
        "issue_identifier": issue.identifier or "issue",
    }


def before_complete_update(
    event: str,
    hook_result: HookCommandResult,
    *,
    gate_source: str | None = None,
    gate_name: str | None = None,
) -> dict[str, Any]:
    payload = {
        "event": event,
        "timestamp": datetime.now(UTC),
        "status": hook_result.status,
        "stdout": hook_result.stdout,
        "stderr": hook_result.stderr,
    }
    if gate_source is not None:
        payload["gate_source"] = gate_source
    if gate_name is not None:
        payload["gate_name"] = gate_name
    return payload


def before_complete_exhausted_summary(stderr: str, max_feedback_loops: int) -> str:
    detail = stderr.strip().splitlines()[0] if stderr.strip() else "unknown failure"
    return (
        "Code Factory exhausted before_complete repair loops after "
        f"{max_feedback_loops} attempt(s). Last error: {detail}"
    )


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


async def emit_before_complete_update(
    queue: asyncio.Queue[Any],
    issue_id: str | None,
    event: str,
    hook_result: HookCommandResult,
    *,
    gate_source: str | None = None,
    gate_name: str | None = None,
) -> None:
    if issue_id:
        await queue.put(
            AgentWorkerUpdate(
                issue_id,
                before_complete_update(
                    event,
                    hook_result,
                    gate_source=gate_source,
                    gate_name=gate_name,
                ),
            )
        )


async def run_pre_complete_turns(
    *,
    run_turn: Callable[[str], Awaitable[StructuredTurnResult]],
    settings: Settings,
    workspace_path: str | None,
    issue: Issue,
    profile: WorkflowStateProfile,
    queue: asyncio.Queue[Any],
    issue_id: str | None,
    failure_state: str,
    initial_prompt: str,
    should_stop: Callable[[], bool],
) -> StructuredTurnResult:
    """Run agent turns until completion gates accept or exhaust retries."""

    prompt = initial_prompt
    feedback_attempts = 0
    while True:
        result = await run_turn(prompt)
        if should_stop():
            return result
        if result.decision != "transition":
            return result
        if workspace_path is None:
            raise RuntimeError("missing_workspace_for_before_complete")
        native_result = await native_readiness_result(workspace_path, issue, profile)
        if native_result is not None:
            if native_result.status != 0:
                await emit_before_complete_update(
                    queue,
                    issue_id,
                    "before_complete_blocked",
                    native_result,
                    gate_source="native",
                    gate_name="transition_readiness",
                )
                feedback_attempts += 1
                if feedback_attempts > profile.hooks.before_complete_max_feedback_loops:
                    return StructuredTurnResult(
                        decision="blocked",
                        summary=before_complete_exhausted_summary(
                            native_result.stderr,
                            profile.hooks.before_complete_max_feedback_loops,
                        ),
                        next_state=failure_state,
                    )
                prompt = before_complete_feedback_prompt(
                    result,
                    native_result.stderr,
                    feedback_attempts,
                    profile.hooks.before_complete_max_feedback_loops,
                    gate_source="native",
                    gate_name="transition_readiness",
                )
                continue
            await emit_before_complete_update(
                queue,
                issue_id,
                "before_complete_passed",
                native_result,
                gate_source="native",
                gate_name="transition_readiness",
            )
        hook = profile.hooks.before_complete
        if not hook:
            return result
        hook_result = await run_before_complete_hook(
            settings,
            hook,
            workspace_path,
            issue,
            result,
        )
        if hook_result.status == 0:
            await emit_before_complete_update(
                queue,
                issue_id,
                "before_complete_passed",
                hook_result,
                gate_source="hook",
                gate_name="before_complete",
            )
            return result
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
            return result
        await emit_before_complete_update(
            queue,
            issue_id,
            "before_complete_blocked",
            hook_result,
            gate_source="hook",
            gate_name="before_complete",
        )
        feedback_attempts += 1
        if feedback_attempts > profile.hooks.before_complete_max_feedback_loops:
            return StructuredTurnResult(
                decision="blocked",
                summary=before_complete_exhausted_summary(
                    hook_result.stderr,
                    profile.hooks.before_complete_max_feedback_loops,
                ),
                next_state=failure_state,
            )
        prompt = before_complete_feedback_prompt(
            result,
            hook_result.stderr,
            feedback_attempts,
            profile.hooks.before_complete_max_feedback_loops,
            gate_source="hook",
            gate_name="before_complete",
        )
