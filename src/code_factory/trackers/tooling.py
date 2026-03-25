"""Generic tracker-backed ticket operations for CLI and agent tool surfaces."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from ..config.models import Settings
from ..errors import TrackerClientError


class TrackerOps(Protocol):
    """Normalized ticket-operations surface shared by CLI and dynamic tools."""

    async def close(self) -> None: ...

    async def raw_graphql(self, query: str, variables: dict | None = None) -> dict: ...

    async def read_issue(
        self,
        issue: str,
        *,
        include_description: bool,
        include_comments: bool,
        include_attachments: bool,
        include_relations: bool,
    ) -> dict: ...

    async def read_issues(
        self,
        *,
        project: str | None,
        state: str | None,
        query: str | None,
        limit: int,
        include_description: bool,
        include_comments: bool,
        include_attachments: bool,
        include_relations: bool,
    ) -> dict: ...

    async def read_project(self, project: str) -> dict: ...

    async def read_projects(self, *, query: str | None, limit: int) -> dict: ...

    async def read_states(
        self,
        *,
        issue: str | None,
        team: str | None,
        project: str | None,
    ) -> dict: ...

    async def list_comments(self, issue: str) -> dict: ...

    async def create_comment(self, issue: str, body: str) -> dict: ...

    async def update_comment(self, comment_id: str, body: str) -> dict: ...

    async def get_workpad(self, issue: str) -> dict: ...

    async def sync_workpad(
        self,
        issue: str,
        *,
        body: str | None = None,
        file_path: str | None = None,
    ) -> dict: ...

    async def move_issue(self, issue: str, state: str) -> dict: ...

    async def create_issue(self, **kwargs: object) -> dict: ...

    async def update_issue(self, issue: str, **kwargs: object) -> dict: ...

    async def link_pr(self, issue: str, url: str, title: str | None) -> dict: ...

    async def upload_file(self, file_path: str) -> dict: ...


class UnsupportedTrackerOps:
    """Placeholder used when the workflow tracker does not support ticket ops."""

    def __init__(self, tracker_kind: str | None) -> None:
        self._tracker_kind = tracker_kind or "unknown"

    async def close(self) -> None:
        return None

    def __getattr__(self, _name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
        async def _unsupported(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise TrackerClientError(
                (
                    "tracker_operation_failed",
                    f"tracker operations require a Linear-backed workflow, got `{self._tracker_kind}`",
                )
            )

        return _unsupported


def build_tracker_ops(
    settings: Settings, *, allowed_roots: tuple[str, ...] = ()
) -> TrackerOps:
    """Build the normalized ticket-operations adapter for the active tracker."""

    if settings.tracker.kind == "linear":
        from .linear import LinearOps

        return LinearOps.from_settings(settings, allowed_roots=allowed_roots)
    return cast(TrackerOps, UnsupportedTrackerOps(settings.tracker.kind))
