from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from symphony.config import parse_settings
from symphony.issues import Issue
from symphony.workflow import WorkflowSnapshot, current_stamp, load_workflow

DEFAULT_PROMPT = "You are an agent for this repository."


def default_workflow_config() -> dict[str, Any]:
    return {
        "tracker": {
            "kind": "linear",
            "endpoint": "https://api.linear.app/graphql",
            "api_key": "token",
            "project_slug": "project",
            "assignee": None,
            "active_states": ["Todo", "In Progress"],
            "terminal_states": ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"],
        },
        "polling": {"interval_ms": 30_000},
        "workspace": {"root": os.path.join("/tmp", "symphony_workspaces")},
        "agent": {
            "max_concurrent_agents": 10,
            "max_turns": 20,
            "max_retry_backoff_ms": 300_000,
            "max_concurrent_agents_by_state": {},
        },
        "codex": {
            "command": "codex app-server",
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
        "hooks": {"timeout_ms": 60_000},
        "observability": {
            "dashboard_enabled": True,
            "refresh_ms": 1_000,
            "render_interval_ms": 16,
        },
        "server": {"port": None, "host": "127.0.0.1"},
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
    config = deep_merge(default_workflow_config(), overrides)
    yaml_body = yaml.safe_dump(config, sort_keys=False)
    path.write_text(f"---\n{yaml_body}---\n{prompt}\n", encoding="utf-8")
    return path


def make_snapshot(workflow_path: Path) -> WorkflowSnapshot:
    definition = load_workflow(str(workflow_path))
    settings = parse_settings(definition.config)
    return WorkflowSnapshot(
        version=1,
        path=str(workflow_path),
        stamp=current_stamp(str(workflow_path)),
        definition=definition,
        settings=settings,
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
