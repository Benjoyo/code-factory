from __future__ import annotations

"""Shared prompt/update helpers for pre-completion quality gates."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from ....issues import Issue
from ....structured_results import StructuredTurnResult
from ....workspace.hooks import HookCommandResult
from ...messages import AgentWorkerUpdate

BEFORE_COMPLETE_STDERR_LIMIT = 12_000


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


def before_complete_exhausted_summary(stderr: str, max_feedback_loops: int) -> str:
    detail = stderr.strip().splitlines()[0] if stderr.strip() else "unknown failure"
    return (
        "Code Factory exhausted before_complete repair loops after "
        f"{max_feedback_loops} attempt(s). Last error: {detail}"
    )


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


async def emit_before_complete_update(
    queue: asyncio.Queue[object],
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
