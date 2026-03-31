from __future__ import annotations

from pathlib import Path


def test_default_workflow_template_includes_workpad_qa_plan_guidance() -> None:
    template = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "code_factory"
        / "workflow"
        / "templates"
        / "default.md"
    ).read_text(encoding="utf-8")

    assert "`Acceptance Criteria`, `QA Plan`, and `Validation`" in template
    assert "Fill in `QA Plan` with operator-facing manual test scenarios" in template
    assert "`Plan`, `Acceptance Criteria`, `QA Plan`, and `Validation`" in template
