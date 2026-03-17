from __future__ import annotations

"""Configuration helpers tailored to the Linear tracker integration."""

import os
from collections.abc import Mapping
from typing import Any

from ...config.models import Settings, TrackerSettings
from ...config.utils import (
    optional_string,
    require_mapping,
    resolve_secret_setting,
    string_list,
    string_with_default,
)
from ...errors import ConfigValidationError


def supports_tracker_kind(kind: str | None) -> bool:
    """Linear is the only kind this module understands."""
    return kind == "linear"


def validate_tracker_settings(settings: Settings) -> None:
    if not settings.tracker.api_key:
        raise ConfigValidationError(
            "LINEAR_API_KEY is required", code="missing_linear_api_token"
        )
    if not settings.tracker.project_slug:
        raise ConfigValidationError(
            "tracker.project_slug is required", code="missing_linear_project_slug"
        )


def parse_tracker_settings(config: Mapping[str, Any] | Any) -> TrackerSettings:
    # Keep parsing tautologically simple so we can trust defaults.
    tracker_raw = (
        require_mapping(config.get("tracker"), "tracker")
        if isinstance(config, Mapping)
        else {}
    )
    return TrackerSettings(
        kind=optional_string(tracker_raw.get("kind"), "tracker.kind"),
        endpoint=string_with_default(
            tracker_raw.get("endpoint"),
            "tracker.endpoint",
            "https://api.linear.app/graphql",
        ),
        api_key=resolve_secret_setting(
            tracker_raw.get("api_key"),
            os.getenv("LINEAR_API_KEY"),
            "tracker.api_key",
        ),
        project_slug=optional_string(
            tracker_raw.get("project_slug"), "tracker.project_slug"
        ),
        assignee=resolve_secret_setting(
            tracker_raw.get("assignee"),
            os.getenv("LINEAR_ASSIGNEE"),
            "tracker.assignee",
        ),
        active_states=string_list(
            tracker_raw.get("active_states"),
            "tracker.active_states",
            ("Todo", "In Progress"),
        ),
        terminal_states=string_list(
            tracker_raw.get("terminal_states"),
            "tracker.terminal_states",
            ("Closed", "Cancelled", "Canceled", "Duplicate", "Done"),
        ),
    )
