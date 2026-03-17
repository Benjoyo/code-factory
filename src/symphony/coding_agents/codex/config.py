from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...config.models import CodingAgentSettings, Settings
from ...config.utils import (
    non_negative_int,
    normalize_keys,
    positive_int,
    require_mapping,
    required_command,
    string_with_default,
)
from ...errors import ConfigValidationError


def validate_coding_agent_settings(settings: Settings) -> None:
    if settings.coding_agent.command == "":
        raise ConfigValidationError("codex.command can't be blank")


def parse_coding_agent_settings(config: Mapping[str, Any]) -> CodingAgentSettings:
    runtime_raw = require_mapping(config.get("codex"), "codex")
    approval_policy = normalize_approval_policy(runtime_raw.get("approval_policy"))
    turn_sandbox_policy = runtime_raw.get("turn_sandbox_policy")
    if turn_sandbox_policy is not None and not isinstance(turn_sandbox_policy, Mapping):
        raise ConfigValidationError("codex.turn_sandbox_policy must be an object")

    return CodingAgentSettings(
        command=required_command(
            runtime_raw.get("command"), "codex.command", "codex app-server"
        ),
        approval_policy=approval_policy,
        thread_sandbox=string_with_default(
            runtime_raw.get("thread_sandbox"),
            "codex.thread_sandbox",
            "workspace-write",
        ),
        turn_sandbox_policy=normalize_keys(turn_sandbox_policy)
        if isinstance(turn_sandbox_policy, Mapping)
        else None,
        turn_timeout_ms=positive_int(
            runtime_raw.get("turn_timeout_ms"), "codex.turn_timeout_ms", 3_600_000
        ),
        read_timeout_ms=positive_int(
            runtime_raw.get("read_timeout_ms"), "codex.read_timeout_ms", 5_000
        ),
        stall_timeout_ms=non_negative_int(
            runtime_raw.get("stall_timeout_ms"), "codex.stall_timeout_ms", 300_000
        ),
    )


def normalize_approval_policy(approval_policy: Any) -> str | dict[str, Any]:
    if approval_policy is None:
        return {
            "reject": {
                "sandbox_approval": True,
                "rules": True,
                "mcp_elicitations": True,
            }
        }
    if not isinstance(approval_policy, str | Mapping):
        raise ConfigValidationError("codex.approval_policy must be a string or object")
    return normalize_keys(approval_policy)
