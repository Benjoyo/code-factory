"""Config parsing helpers that coerce the workflow document into typed models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..coding_agents.base import parse_coding_agent_settings
from ..errors import ConfigValidationError
from ..trackers.base import parse_tracker_settings
from .defaults import (
    DEFAULT_PROMPT_TEMPLATE,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    DEFAULT_WORKSPACE_ROOT,
)
from .models import (
    AgentSettings,
    HooksSettings,
    ObservabilitySettings,
    PollingSettings,
    ServerSettings,
    Settings,
    WorkspaceSettings,
)
from .review import parse_review_settings
from .utils import (
    boolean,
    normalize_state_limits,
    optional_non_blank_string,
    optional_non_negative_int,
    optional_string,
    positive_int,
    require_mapping,
    resolve_path_value,
    string_list,
    string_with_default,
)


def parse_settings(config: Mapping[str, Any]) -> Settings:
    """Turn the flattened mapping into a validated `Settings` graph used everywhere else."""

    tracker = parse_tracker_settings(config)
    polling_raw = require_mapping(config.get("polling"), "polling")
    workspace_raw = require_mapping(config.get("workspace"), "workspace")
    agent_raw = require_mapping(config.get("agent"), "agent")
    hooks_raw = require_mapping(config.get("hooks"), "hooks")
    observability_raw = require_mapping(config.get("observability"), "observability")
    server_raw = require_mapping(config.get("server"), "server")
    _reject_unsupported_keys(
        agent_raw,
        "agent",
        {
            "max_concurrent_agents",
            "max_retry_backoff_ms",
            "max_worker_retries",
            "max_concurrent_agents_by_state",
        },
    )
    failure_state = optional_non_blank_string(
        config.get("failure_state"), "failure_state"
    )
    if failure_state is None:
        raise ConfigValidationError("failure_state is required")

    return Settings(
        failure_state=failure_state,
        terminal_states=string_list(
            config.get("terminal_states"),
            "terminal_states",
            ("Closed", "Cancelled", "Canceled", "Duplicate", "Done"),
        ),
        tracker=tracker,
        polling=PollingSettings(
            interval_ms=positive_int(
                polling_raw.get("interval_ms"), "polling.interval_ms", 30_000
            )
        ),
        workspace=WorkspaceSettings(
            root=resolve_path_value(
                workspace_raw.get("root"), DEFAULT_WORKSPACE_ROOT, "workspace.root"
            )
        ),
        agent=AgentSettings(
            max_concurrent_agents=positive_int(
                agent_raw.get("max_concurrent_agents"),
                "agent.max_concurrent_agents",
                10,
            ),
            max_retry_backoff_ms=positive_int(
                agent_raw.get("max_retry_backoff_ms"),
                "agent.max_retry_backoff_ms",
                300_000,
            ),
            max_worker_retries=positive_int(
                agent_raw.get("max_worker_retries"),
                "agent.max_worker_retries",
                3,
            ),
            max_concurrent_agents_by_state=normalize_state_limits(
                agent_raw.get("max_concurrent_agents_by_state"),
                "agent.max_concurrent_agents_by_state",
            ),
        ),
        coding_agent=parse_coding_agent_settings(config),
        hooks=HooksSettings(
            after_create=optional_string(
                hooks_raw.get("after_create"), "hooks.after_create"
            ),
            before_run=optional_string(hooks_raw.get("before_run"), "hooks.before_run"),
            after_run=optional_string(hooks_raw.get("after_run"), "hooks.after_run"),
            before_remove=optional_string(
                hooks_raw.get("before_remove"), "hooks.before_remove"
            ),
            timeout_ms=positive_int(
                hooks_raw.get("timeout_ms"), "hooks.timeout_ms", 900_000
            ),
        ),
        observability=ObservabilitySettings(
            dashboard_enabled=boolean(
                observability_raw.get("dashboard_enabled"),
                "observability.dashboard_enabled",
                True,
            ),
            refresh_ms=positive_int(
                observability_raw.get("refresh_ms"), "observability.refresh_ms", 1_000
            ),
            render_interval_ms=positive_int(
                observability_raw.get("render_interval_ms"),
                "observability.render_interval_ms",
                16,
            ),
        ),
        server=ServerSettings(
            port=optional_non_negative_int(
                server_raw.get("port"), "server.port", DEFAULT_SERVER_PORT
            ),
            host=string_with_default(
                server_raw.get("host"), "server.host", DEFAULT_SERVER_HOST
            ),
        ),
        review=parse_review_settings(config),
    )


def workflow_prompt(prompt_template: str) -> str:
    """Apply sensible defaults when workflows omit a prompt template string."""

    return prompt_template.strip() or DEFAULT_PROMPT_TEMPLATE


def _reject_unsupported_keys(
    config: Mapping[str, Any], field_name: str, supported_keys: set[str]
) -> None:
    unexpected_keys = set(config.keys()) - supported_keys
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"{field_name} has unsupported keys: {names}")
