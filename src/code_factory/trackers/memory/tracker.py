from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from ...issues import Issue, normalize_issue_state


class MemoryTracker:
    def __init__(
        self,
        issues: list[Issue] | None = None,
        *,
        recipient: asyncio.Queue[Any] | None = None,
    ) -> None:
        self._issues = list(issues or [])
        self._recipient = recipient

    def replace_issues(self, issues: list[Issue]) -> None:
        self._issues = list(issues)

    async def fetch_candidate_issues(self) -> list[Issue]:
        return list(self._issues)

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
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
        self._issues = [
            replace(issue, state=state_name) if issue.id == issue_id else issue
            for issue in self._issues
        ]


def build_tracker(_settings, **kwargs: Any) -> MemoryTracker:
    return MemoryTracker(**kwargs)
