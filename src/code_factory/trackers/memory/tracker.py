from __future__ import annotations

"""Ephemeral tracker that makes it easy to drive unit tests without external APIs."""

import asyncio
from dataclasses import replace
from typing import Any

from ...issues import Issue, normalize_issue_state


class MemoryTracker:
    """In-memory tracker used when we want deterministic behavior without Linear."""

    def __init__(
        self,
        issues: list[Issue] | None = None,
        *,
        recipient: asyncio.Queue[Any] | None = None,
    ) -> None:
        self._issues = list(issues or [])
        self._recipient = recipient

    def replace_issues(self, issues: list[Issue]) -> None:
        """Replace the full issue snapshot, primarily for tests that mutate inputs."""

        self._issues = list(issues)

    async def fetch_candidate_issues(self) -> list[Issue]:
        return list(self._issues)

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        # Normalize to compare states in a case-insensitive, stable way.
        normalized = {normalize_issue_state(state_name) for state_name in state_names}
        return [
            issue
            for issue in self._issues
            if normalize_issue_state(issue.state) in normalized
        ]

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        wanted = set(issue_ids)
        return [issue for issue in self._issues if issue.id in wanted]

    async def create_comment(self, issue_id: str, body: str) -> None:
        if self._recipient is not None:
            await self._recipient.put(("memory_tracker_comment", issue_id, body))

    async def update_issue_state(self, issue_id: str, state_name: str) -> None:
        if self._recipient is not None:
            await self._recipient.put(
                ("memory_tracker_state_update", issue_id, state_name)
            )
        # Maintain the snapshot so future reads reflect the latest state.
        self._issues = [
            replace(issue, state=state_name) if issue.id == issue_id else issue
            for issue in self._issues
        ]


def build_tracker(_settings, **kwargs: Any) -> MemoryTracker:
    """Keep the constructor signature compatible with the real tracker entrypoints."""

    return MemoryTracker(**kwargs)
