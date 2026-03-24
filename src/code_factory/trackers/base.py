"""Shared tracker boundary helpers used by every coding-agent run."""

from __future__ import annotations

from typing import Any, Protocol

from ..config.models import Settings, TrackerSettings
from ..errors import ConfigValidationError
from ..issues import Issue, IssueComment


class Tracker(Protocol):
    """API that orchestrators depend on when interacting with trackers."""

    async def fetch_candidate_issues(self) -> list[Issue]: ...

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]: ...

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]: ...

    async def fetch_issue_by_identifier(self, identifier: str) -> Issue | None: ...

    async def fetch_issue_comments(self, issue_id: str) -> list[IssueComment]: ...

    async def create_comment(self, issue_id: str, body: str) -> None: ...

    async def update_comment(self, comment_id: str, body: str) -> None: ...

    async def update_issue_state(self, issue_id: str, state_name: str) -> None: ...


def build_tracker(settings: Settings, **kwargs: Any) -> Tracker:
    # Concrete trackers register themselves as build helpers so we can stay decoupled.
    if settings.tracker.kind == "memory":
        from .memory.tracker import build_tracker as build_memory_tracker

        return build_memory_tracker(settings, **kwargs)

    from .linear.client import build_tracker as build_linear_tracker

    return build_linear_tracker(settings, **kwargs)


def validate_tracker_settings(settings: Settings) -> None:
    kind = settings.tracker.kind
    if kind is None:
        raise ConfigValidationError(
            "tracker.kind is required", code="missing_tracker_kind"
        )
    if kind == "memory":
        return

    from .linear.config import supports_tracker_kind
    from .linear.config import validate_tracker_settings as validate_linear_settings

    if not supports_tracker_kind(kind):
        raise ConfigValidationError(
            f"unsupported tracker kind: {kind}",
            code="unsupported_tracker_kind",
        )
    validate_linear_settings(settings)


def parse_tracker_settings(config: dict[str, Any] | Any) -> TrackerSettings:
    from .linear.config import parse_tracker_settings as parse_linear_settings

    tracker = parse_linear_settings(config)
    if tracker.kind == "memory":
        return tracker
    return tracker
