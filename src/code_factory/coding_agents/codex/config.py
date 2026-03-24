from __future__ import annotations

"""Codex runtime configuration with stringent defaults for safe execution."""

import json
import os
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...config.models import CodingAgentSettings, Settings
from ...config.utils import (
    non_negative_int,
    normalize_keys,
    optional_non_blank_string,
    positive_int,
    require_mapping,
    required_command,
    string_with_default,
)
from ...errors import ConfigValidationError


def validate_coding_agent_settings(settings: Settings) -> None:
    if settings.coding_agent.command == "":
        raise ConfigValidationError("codex.command can't be blank")
    build_launch_command(settings.coding_agent)


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
        model=optional_non_blank_string(runtime_raw.get("model"), "codex.model"),
        reasoning_effort=optional_non_blank_string(
            runtime_raw.get("reasoning_effort"), "codex.reasoning_effort"
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


def build_launch_command(
    settings: CodingAgentSettings, workspace: str | None = None
) -> str:
    """Insert workflow-managed CLI flags ahead of the `app-server` subcommand."""

    if (
        settings.model is None
        and settings.reasoning_effort is None
        and settings.repo_skill_allowlist is None
    ):
        return settings.command
    try:
        command_parts = shlex.split(settings.command)
    except ValueError as exc:
        raise ConfigValidationError(
            "codex.command must be a valid shell-style command when codex.model "
            "or codex.reasoning_effort is set"
        ) from exc
    try:
        app_server_index = command_parts.index("app-server")
    except ValueError as exc:
        raise ConfigValidationError(
            "codex.command must include an `app-server` argument when codex.model "
            "or codex.reasoning_effort is set"
        ) from exc
    injected_flags: list[str] = []
    if settings.reasoning_effort is not None:
        injected_flags.extend(
            ["--config", f"model_reasoning_effort={settings.reasoning_effort}"]
        )
    if settings.model is not None:
        injected_flags.extend(["--model", settings.model])
    if settings.repo_skill_allowlist is not None:
        disabled_skill_paths = _disabled_repo_skill_paths(
            workspace, settings.repo_skill_allowlist
        )
        if disabled_skill_paths:
            injected_flags.extend(
                [
                    "--config",
                    repo_skill_disable_config(disabled_skill_paths),
                ]
            )
    command_parts[app_server_index:app_server_index] = injected_flags
    return shlex.join(command_parts)


def repo_skill_disable_config(skill_paths: tuple[str, ...]) -> str:
    entries = ", ".join(
        f"{{path={json.dumps(skill_path)}, enabled=false}}"
        for skill_path in skill_paths
    )
    return f"skills.config=[{entries}]"


def _disabled_repo_skill_paths(
    workspace: str | None, allowlist: tuple[str, ...]
) -> tuple[str, ...]:
    if workspace is None:
        raise ConfigValidationError(
            "workspace is required when codex repo skill filtering is enabled"
        )
    skill_dirs = _repo_skill_directories(workspace)
    missing = sorted(set(allowlist) - set(skill_dirs))
    if missing:
        names = ", ".join(missing)
        raise ConfigValidationError(
            f"codex.skills references missing repo skills for workspace {workspace}: {names}"
        )
    disabled = [
        str(skill_dirs[skill_name] / "SKILL.md")
        for skill_name in sorted(skill_dirs)
        if skill_name not in allowlist
    ]
    return tuple(disabled)


def _repo_skill_directories(workspace: str) -> dict[str, Path]:
    skills_root = (
        Path(os.path.abspath(os.path.expanduser(workspace))) / ".agents" / "skills"
    )
    if not skills_root.is_dir():
        return {}
    skill_dirs: dict[str, Path] = {}
    for child in sorted(skills_root.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        if not (child / "SKILL.md").is_file():
            continue
        skill_dirs[child.name] = child
    return skill_dirs


def normalize_approval_policy(approval_policy: Any) -> str | dict[str, Any]:
    """Make rejection by policy the safe default unless the user configures otherwise."""
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
