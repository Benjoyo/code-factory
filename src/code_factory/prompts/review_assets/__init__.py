from __future__ import annotations

"""Vendored Codex review prompt and schema owned by Code Factory."""

import json
from importlib.resources import files
from typing import Any


def base_review_prompt() -> str:
    """Return the vendored Codex review base prompt verbatim."""

    return (
        files("code_factory.prompts.review_assets")
        .joinpath("codex-review-base.md")
        .read_text(encoding="utf-8")
    )


def review_output_schema() -> dict[str, Any]:
    """Return the vendored Codex review output schema."""

    return json.loads(
        files("code_factory.prompts.review_assets")
        .joinpath("review-output.json")
        .read_text(encoding="utf-8")
    )
