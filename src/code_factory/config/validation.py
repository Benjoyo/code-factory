from __future__ import annotations

from ..coding_agents.base import validate_coding_agent_settings
from ..issues import normalize_issue_state
from ..trackers.base import validate_tracker_settings
from .models import Settings


def validate_dispatch_settings(settings: Settings) -> None:
    validate_tracker_settings(settings)
    validate_coding_agent_settings(settings)


def max_concurrent_agents_for_state(settings: Settings, state_name: str | None) -> int:
    if state_name:
        normalized = normalize_issue_state(state_name)
        limit = settings.agent.max_concurrent_agents_by_state.get(normalized)
        if isinstance(limit, int) and limit > 0:
            return limit
    return settings.agent.max_concurrent_agents
