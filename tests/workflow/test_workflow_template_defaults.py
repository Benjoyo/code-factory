from __future__ import annotations

from pathlib import Path


def test_default_workflow_template_includes_workpad_qa_plan_guidance() -> None:
    template = (
        Path(__file__).resolve().parents[1].parent
        / "src"
        / "code_factory"
        / "workflow"
        / "templates"
        / "default.md"
    ).read_text(encoding="utf-8")

    assert "Add explicit acceptance criteria and TODOs in checklist form" in template
    assert "copy those requirements into the workpad `Acceptance Criteria`" in template
    assert (
        "`Plan`, `Acceptance Criteria`, and `Validation` exactly match completed work"
        in template
    )
    assert "embed the returned Markdown" in template
    assert "Do not wrap uploaded media Markdown in backticks" in template
    assert "summary` is a downstream handoff artifact" in template
    assert "Exclude operational noise from `summary`" in template
    assert "not as an activity log of branch/PR/test/review actions" in template
