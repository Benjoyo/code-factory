from __future__ import annotations

from ...config.models import Settings
from ...issues import normalize_issue_state


def tracker_state_is_active(settings: Settings, state_name: str | None) -> bool:
    normalized = normalize_issue_state(state_name)
    return normalized in {
        normalize_issue_state(state) for state in settings.tracker.active_states
    }
