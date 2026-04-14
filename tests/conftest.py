from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from code_factory.config import parse_settings
from code_factory.issues import Issue
from code_factory.workflow import WorkflowSnapshot, current_stamp, load_workflow
from code_factory.workflow.profiles.review_profiles import parse_review_types
from code_factory.workflow.profiles.state_profiles import parse_state_profiles

DEFAULT_PROMPT = "# prompt: default\nYou are an agent for this repository."


def default_workflow_config() -> dict[str, Any]:
    return {
        "failure_state": "Human Review",
        "terminal_states": [
            "Closed",
            "Cancelled",
            "Canceled",
            "Duplicate",
            "Done",
        ],
        "tracker": {
            "kind": "linear",
            "endpoint": "https://api.linear.app/graphql",
            "api_key": "token",
            "project": "project",
            "assignee": None,
        },
        "states": {
            "Todo": {"auto_next_state": "In Progress"},
            "In Progress": {"prompt": "default"},
        },
        "polling": {"interval_ms": 30_000},
        "workspace": {"root": os.path.join("/tmp", "code-factory-workspaces")},
        "agent": {
            "max_concurrent_agents": 10,
            "max_retry_backoff_ms": 300_000,
            "max_worker_retries": 3,
            "max_concurrent_agents_by_state": {},
        },
        "codex": {
            "command": "codex app-server",
            "model": None,
            "reasoning_effort": None,
            "fast_mode": None,
            "approval_policy": {
                "reject": {
                    "sandbox_approval": True,
                    "rules": True,
                    "mcp_elicitations": True,
                }
            },
            "thread_sandbox": "workspace-write",
            "turn_sandbox_policy": None,
            "turn_timeout_ms": 3_600_000,
            "read_timeout_ms": 5_000,
            "stall_timeout_ms": 300_000,
        },
        "hooks": {"timeout_ms": 900_000},
        "observability": {
            "dashboard_enabled": True,
            "refresh_ms": 1_000,
            "render_interval_ms": 16,
        },
        "server": {"port": 4000, "host": "127.0.0.1"},
    }


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def write_workflow_file(
    path: Path, *, prompt: str = DEFAULT_PROMPT, **overrides: Any
) -> Path:
    config = deep_merge(
        default_workflow_config(),
        {key: value for key, value in overrides.items() if key != "states"},
    )
    if "states" in overrides:
        config["states"] = copy.deepcopy(overrides["states"])
    yaml_body = yaml.safe_dump(config, sort_keys=False)
    rendered_prompt = (
        prompt
        if "# prompt:" in prompt
        else f"# prompt: default\n{prompt}".rstrip() + "\n"
    )
    path.write_text(f"---\n{yaml_body}---\n{rendered_prompt}\n", encoding="utf-8")
    return path


def make_snapshot(workflow_path: Path) -> WorkflowSnapshot:
    definition = load_workflow(str(workflow_path))
    settings = parse_settings(definition.config)
    ai_review_types = parse_review_types(definition.config, definition.review_sections)
    state_profiles = parse_state_profiles(
        definition.config, definition.prompt_sections, ai_review_types
    )
    return WorkflowSnapshot(
        version=1,
        path=str(workflow_path),
        stamp=current_stamp(str(workflow_path)),
        definition=definition,
        settings=settings,
        state_profiles=state_profiles,
        ai_review_types=ai_review_types,
    )


@pytest.fixture
def workflow_path(tmp_path: Path) -> Path:
    return write_workflow_file(tmp_path / "WORKFLOW.md")


def make_issue(**overrides: Any) -> Issue:
    base = {
        "id": "issue-1",
        "identifier": "MT-1",
        "title": "Test issue",
        "description": "Test body",
        "state": "In Progress",
        "url": "https://example.org/issues/MT-1",
        "labels": ("backend",),
    }
    base.update(overrides)
    return Issue(**base)
