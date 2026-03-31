"""Shared runtime activity-phase labels for worker progress observability."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from .messages import AgentWorkerUpdate

EXECUTION_PHASE = "Execution"
QUALITY_GATES_PHASE = "Quality Gates"
AI_REVIEW_PHASE = "AI Review"


async def emit_activity_phase_update(
    queue: asyncio.Queue[object],
    issue_id: str | None,
    *,
    event: str,
    activity_phase: str,
) -> None:
    """Publish an observability-only phase transition for a running issue."""

    if issue_id:
        await queue.put(
            AgentWorkerUpdate(
                issue_id,
                {
                    "event": event,
                    "timestamp": datetime.now(UTC),
                    "activity_phase": activity_phase,
                },
            )
        )
