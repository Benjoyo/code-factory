from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from ...issues import Issue
from ...structured_results import StructuredTurnResult
from ...workspace.hooks import HookCommandResult
from ..messages import AgentWorkerUpdate

BEFORE_COMPLETE_STDERR_LIMIT = 12_000


def before_complete_feedback_prompt(
    result: StructuredTurnResult,
    stderr: str,
    attempt: int,
    max_attempts: int,
) -> str:
    feedback = stderr.strip() or (
        "The before_complete hook exited with status 2 without any stderr output."
    )
    if len(feedback) > BEFORE_COMPLETE_STDERR_LIMIT:
        feedback = (
            f"{feedback[:BEFORE_COMPLETE_STDERR_LIMIT]}\n\n"
            "[truncated: before_complete stderr exceeded 12000 characters]"
        )
    return (
        "A `before_complete` quality gate blocked completion for this workflow state.\n"
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
    event: str, hook_result: HookCommandResult
) -> dict[str, Any]:
    return {
        "event": event,
        "timestamp": datetime.now(UTC),
        "status": hook_result.status,
        "stdout": hook_result.stdout,
        "stderr": hook_result.stderr,
    }


async def emit_before_complete_update(
    queue: asyncio.Queue[Any],
    issue_id: str | None,
    event: str,
    hook_result: HookCommandResult,
) -> None:
    if issue_id:
        await queue.put(
            AgentWorkerUpdate(issue_id, before_complete_update(event, hook_result))
        )
