from __future__ import annotations

from pathlib import Path

import pytest

from code_factory.config import parse_settings
from code_factory.errors import ConfigValidationError
from code_factory.workflow import load_workflow

from .conftest import make_snapshot, write_workflow_file


def test_workflow_snapshot_loads_ai_review_types_and_sections(tmp_path: Path) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        review={
            "temp_root": str(tmp_path / "review-root"),
            "prepare": "pnpm install",
        },
        ai_review={
            "types": {
                "Security": {
                    "prompt": "security",
                    "model": "gpt-5.4-mini",
                    "reasoning_effort": "high",
                    "lines_changed": 25,
                    "paths": {
                        "include": ["src/**"],
                        "exclude": ["tests/**"],
                    },
                },
                "Frontend": {
                    "prompt": "frontend",
                    "paths": {"only": ["web/**", "ui/**"]},
                },
            }
        },
        states={
            "Todo": {"auto_next_state": "In Progress"},
            "In Progress": {
                "prompt": "default",
                "ai_review": [" security ", "Frontend"],
            },
        },
        prompt=(
            "# prompt: default\n"
            "Implement the issue.\n\n"
            "# review: security\n"
            "Find security and permission bugs.\n\n"
            "# review: frontend\n"
            "Focus on UX regressions and unsafe client assumptions.\n"
        ),
    )

    loaded = load_workflow(str(workflow))
    settings = parse_settings(loaded.config)
    snapshot = make_snapshot(workflow)

    assert loaded.prompt_sections == {"default": "Implement the issue."}
    assert loaded.review_sections == {
        "security": "Find security and permission bugs.",
        "frontend": "Focus on UX regressions and unsafe client assumptions.",
    }
    assert settings.review.temp_root == str((tmp_path / "review-root").resolve())
    assert settings.review.prepare == "pnpm install"

    profile = snapshot.state_profile("In Progress")
    assert profile is not None
    assert profile.ai_review_refs == ("security", "frontend")
    assert profile.ai_review_scope == "auto"
    assert profile.resolved_ai_review_scope() == "worktree"

    security_review, frontend_review = snapshot.ai_review_types_for_state("In Progress")
    assert security_review.review_name == "Security"
    assert security_review.prompt_ref == "security"
    assert security_review.model == "gpt-5.4-mini"
    assert security_review.reasoning_effort == "high"
    assert security_review.lines_changed == 25
    assert security_review.paths.include == ("src/**",)
    assert security_review.paths.exclude == ("tests/**",)
    assert frontend_review.review_name == "Frontend"
    assert frontend_review.paths.only == ("web/**", "ui/**")
    assert snapshot.ai_review_type(" frontend ") == frontend_review


def test_workflow_snapshot_loads_state_ai_review_scope_object(tmp_path: Path) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        ai_review={"types": {"Security": {"prompt": "security"}}},
        states={
            "Todo": {"auto_next_state": "In Progress"},
            "In Progress": {
                "prompt": "default",
                "completion": {"require_pushed_head": True},
                "ai_review": {"types": "Security", "scope": "branch"},
            },
            "In Review": {
                "prompt": "default",
                "ai_review": {"types": ["Security"]},
            },
        },
        prompt=(
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n"
        ),
    )

    snapshot = make_snapshot(workflow)

    in_progress = snapshot.state_profile("In Progress")
    assert in_progress is not None
    assert in_progress.ai_review_refs == ("security",)
    assert in_progress.ai_review_scope == "branch"
    assert in_progress.resolved_ai_review_scope() == "branch"

    in_review = snapshot.state_profile("In Review")
    assert in_review is not None
    assert in_review.ai_review_refs == ("security",)
    assert in_review.ai_review_scope == "auto"
    assert in_review.resolved_ai_review_scope() == "worktree"


def test_workflow_snapshot_auto_scope_resolves_to_branch_for_completion_states(
    tmp_path: Path,
) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        ai_review={"types": {"Security": {"prompt": "security"}}},
        states={
            "Todo": {"auto_next_state": "In Progress"},
            "In Progress": {
                "prompt": "default",
                "completion": {"require_pr": True},
                "ai_review": "Security",
            },
        },
        prompt=(
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n"
        ),
    )

    profile = make_snapshot(workflow).state_profile("In Progress")

    assert profile is not None
    assert profile.ai_review_scope == "auto"
    assert profile.resolved_ai_review_scope() == "branch"


@pytest.mark.parametrize(
    ("overrides", "prompt", "message"),
    [
        (
            {
                "ai_review": {"types": {"Security": {"prompt": "missing"}}},
                "states": {
                    "Todo": {"auto_next_state": "In Progress"},
                    "In Progress": {"prompt": "default"},
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "references missing review section 'missing'",
        ),
        (
            {
                "ai_review": {"types": {"Security": {"prompt": "security"}}},
                "states": {
                    "Todo": {"auto_next_state": "In Progress"},
                    "In Progress": {"prompt": "default", "ai_review": "missing"},
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "references missing review type 'missing'",
        ),
        (
            {
                "ai_review": {"types": {"Security": {"prompt": "security"}}},
                "states": {
                    "Todo": {"auto_next_state": "In Progress", "ai_review": "Security"},
                    "In Progress": {"prompt": "default"},
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "states.Todo.ai_review is not supported for auto states",
        ),
        (
            {
                "ai_review": {
                    "types": {
                        "Security": {
                            "prompt": "security",
                            "paths": {"include": []},
                        }
                    }
                },
                "states": {
                    "Todo": {"auto_next_state": "In Progress"},
                    "In Progress": {"prompt": "default"},
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "ai_review.types.Security.paths.include must not be empty",
        ),
        (
            {
                "ai_review": {
                    "types": {
                        "Security": {
                            "prompt": "security",
                            "paths": {"unknown": ["src/**"]},
                        }
                    }
                },
                "states": {
                    "Todo": {"auto_next_state": "In Progress"},
                    "In Progress": {"prompt": "default"},
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "ai_review.types.Security.paths has unsupported keys: unknown",
        ),
        (
            {
                "ai_review": {"types": {"Security": {"prompt": "security"}}},
                "states": {
                    "Todo": {"auto_next_state": "In Progress"},
                    "In Progress": {
                        "prompt": "default",
                        "ai_review": {
                            "types": "Security",
                            "scope": "invalid",
                        },
                    },
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "states.In Progress.ai_review.scope must be one of: auto, branch, worktree",
        ),
        (
            {
                "ai_review": {"types": {"Security": {"prompt": "security"}}},
                "states": {
                    "Todo": {"auto_next_state": "In Progress"},
                    "In Progress": {
                        "prompt": "default",
                        "ai_review": {
                            "types": "Security",
                            "scope": 1,
                        },
                    },
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "states.In Progress.ai_review.scope must be a string",
        ),
        (
            {
                "ai_review": {"types": {"Security": {"prompt": "security"}}},
                "states": {
                    "Todo": {"auto_next_state": "In Progress"},
                    "In Progress": {
                        "prompt": "default",
                        "ai_review": {
                            "scope": "branch",
                        },
                    },
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "states.In Progress.ai_review.types is required",
        ),
        (
            {
                "ai_review": {"types": {"Security": {"prompt": "security"}}},
                "states": {
                    "Todo": {"auto_next_state": "In Progress"},
                    "In Progress": {
                        "prompt": "default",
                        "ai_review": {
                            "types": "Security",
                            "scope": "branch",
                            "extra": True,
                        },
                    },
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "states.In Progress.ai_review has unsupported keys: extra",
        ),
        (
            {
                "ai_review": {"types": {"Security": {"prompt": "security"}}},
                "states": {
                    "Todo": {
                        "auto_next_state": "In Progress",
                        "ai_review": {"types": "Security", "scope": "branch"},
                    },
                    "In Progress": {"prompt": "default"},
                },
            },
            "# prompt: default\nImplement.\n\n# review: security\nCheck security.\n",
            "states.Todo.ai_review is not supported for auto states",
        ),
    ],
)
def test_workflow_ai_review_validation_rejects_invalid_config(
    tmp_path: Path,
    overrides: dict[str, object],
    prompt: str,
    message: str,
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", prompt=prompt, **overrides)

    with pytest.raises(ConfigValidationError, match=message):
        make_snapshot(workflow)
