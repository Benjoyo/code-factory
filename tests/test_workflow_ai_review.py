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
