"""Control-plane helpers for steering actively running issues."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...errors import AppServerError, ControlRequestError
from ..messages import SteerIssueRequest

if TYPE_CHECKING:
    from .context import OrchestratorContext


class ControlMixin:
    async def request_steer(
        self: OrchestratorContext, issue_identifier: str, message: str
    ) -> dict[str, Any]:
        """Ask the orchestrator to steer one actively running issue."""

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self.queue.put(SteerIssueRequest(future, issue_identifier, message))
        return await future

    async def _handle_steer_issue(
        self: OrchestratorContext, message: SteerIssueRequest
    ) -> None:
        issue_identifier = message.issue_identifier
        retry = next(
            (
                entry
                for entry in self.retry_entries.values()
                if entry.identifier == issue_identifier
            ),
            None,
        )
        if retry is not None:
            _raise_error(message.future, "issue_not_steerable", 409, issue_identifier)
            return
        entry = next(
            (
                current
                for current in self.running.values()
                if current.identifier == issue_identifier
            ),
            None,
        )
        if entry is None:
            _raise_error(message.future, "issue_not_found", 404, issue_identifier)
            return
        if entry.stopping or entry.worker is None or entry.turn_id is None:
            _raise_error(message.future, "issue_not_steerable", 409, issue_identifier)
            return
        try:
            accepted_turn_id = await entry.worker.steer(message.message)
        except (AppServerError, RuntimeError) as exc:
            code, status = (
                ("issue_not_steerable", 409)
                if "no_active_turn" in str(exc) or "active_session" in str(exc)
                else ("steer_failed", 502)
            )
            message.future.set_exception(
                ControlRequestError(code, f"{issue_identifier}: {exc}", status)
            )
            return
        message.future.set_result(
            {
                "accepted": True,
                "issue_identifier": issue_identifier,
                "thread_id": entry.thread_id,
                "turn_id": accepted_turn_id or entry.turn_id,
                "accepted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )


def _raise_error(
    future: asyncio.Future[Any], code: str, status: int, issue_identifier: str
) -> None:
    message = {
        "issue_not_found": f"{issue_identifier}: issue not found",
        "issue_not_steerable": f"{issue_identifier}: issue is not currently steerable",
    }.get(code, f"{issue_identifier}: {code}")
    future.set_exception(ControlRequestError(code, message, status))
