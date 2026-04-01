from __future__ import annotations

from code_factory.coding_agents.review_models import (
    ReviewCodeLocation,
    ReviewFinding,
    ReviewLineRange,
    ReviewOutput,
    normalize_review_output,
)
from code_factory.issues import Issue
from code_factory.prompts.review_assets import base_review_prompt
from code_factory.workflow.profiles.review_profiles import WorkflowReviewType
from code_factory.workspace.ai_review.ai_review_feedback import (
    accepted_review_findings,
    ai_review_feedback_prompt,
)
from code_factory.workspace.ai_review.ai_review_prompt import (
    render_ai_review_prompt,
)


def test_ai_review_prompt_starts_with_vendored_base_prompt() -> None:
    prompt = render_ai_review_prompt(
        Issue(
            identifier="BEN-28",
            title="Run manual Codex review turns",
            description="Implement the detached review path.",
            state="In Progress",
            branch_name="codex/ben-28",
            priority=1,
            url="https://linear.app/example/BEN-28",
            labels=("manual-review", "codex"),
        ),
        WorkflowReviewType(review_name="Security", prompt_ref="security"),
        "Look for security regressions first.",
        review_scope="worktree",
        base_ref=None,
        changed_paths=("src/app.py", "tests/test_app.py"),
        lines_changed=17,
    )

    base_prompt = base_review_prompt()
    anchor = (
        "Below are some more detailed guidelines that you should apply to this "
        "specific review."
    )
    prefix, separator, suffix = base_prompt.partition(anchor)
    assert separator
    assert prompt.startswith(prefix.rstrip())
    assert "## Code Factory review context" in prompt
    assert prompt.index("## Code Factory review context") < prompt.index(anchor)
    assert prompt.index("## Code Factory review context") > prompt.index(
        "When flagging a bug, you will also provide an accompanying comment."
    )
    assert prompt.index(anchor) > prompt.index("### Workflow-specific review focus")
    assert suffix in prompt
    assert "- Review type: Security" in prompt
    assert "- Review scope: worktree" in prompt
    assert "- Identifier: BEN-28" in prompt
    assert "- Title: Run manual Codex review turns" in prompt
    assert "- Branch: codex/ben-28" not in prompt
    assert "- Labels: manual-review, codex" not in prompt
    assert "Implement the detached review path." in prompt
    assert "Look for security regressions first." in prompt
    assert "- Lines changed: 17" in prompt
    assert "- Review only the current workspace diff." in prompt
    assert "Changed paths:\n- src/app.py\n- tests/test_app.py" in prompt
    assert prompt.rstrip().endswith(
        "Return only schema-valid JSON that matches the configured output schema."
    )


def test_ai_review_prompt_renders_branch_scope_context() -> None:
    prompt = render_ai_review_prompt(
        Issue(identifier="BEN-28", title="Branch review"),
        WorkflowReviewType(review_name="Security", prompt_ref="security"),
        "Focus on the committed branch patch.",
        review_scope="branch",
        base_ref="origin/main",
        changed_paths=("src/app.py",),
        lines_changed=12,
    )

    assert "- Review scope: branch" in prompt
    assert (
        "- Review the committed branch diff from the merge-base with `origin/main` to `HEAD`."
        in prompt
    )
    assert "Focus on the committed branch patch." in prompt


def test_review_model_accepts_vendored_correctness_and_nullable_priority() -> None:
    normalized = normalize_review_output(
        {
            "findings": [
                {
                    "title": "[P1] Broken guard",
                    "body": "Null access can crash this path.",
                    "confidence_score": 0.92,
                    "priority": None,
                    "code_location": {
                        "absolute_file_path": "/tmp/workspace/app.py",
                        "line_range": {"start": 11, "end": 12},
                    },
                }
            ],
            "overall_correctness": "patch is incorrect",
            "overall_explanation": "A correctness issue remains.",
            "overall_confidence_score": 0.88,
        }
    )

    assert normalized is not None
    assert normalized.overall_correctness == "patch is incorrect"
    assert normalized.findings[0].priority is None


def test_ai_review_feedback_filters_low_confidence_findings() -> None:
    review_output = ReviewOutput(
        findings=(
            _finding("Keep", 0.91, "/tmp/a.py", 10, 12),
            _finding("Drop", 0.40, "/tmp/b.py", 4, 4),
        ),
        overall_correctness="incorrect",
        overall_explanation="There is one real issue.",
        overall_confidence_score=0.84,
    )

    accepted = accepted_review_findings(review_output)

    assert [finding.title for finding in accepted] == ["Keep"]
    feedback = ai_review_feedback_prompt(
        findings=accepted,
        review_types=(
            WorkflowReviewType(review_name="Security", prompt_ref="security"),
        ),
        attempt=1,
        max_attempts=3,
    )
    assert "Triggered review types: Security." in feedback
    assert "keep `summary` global to the entire workflow-state run" in feedback
    assert "Exclude operational noise such as branch/PR details" in feedback
    assert "- Keep" in feedback
    assert "/tmp/a.py:10-12" in feedback
    assert "Drop" not in feedback


def test_ai_review_feedback_filters_missing_priority_findings() -> None:
    review_output = ReviewOutput(
        findings=(
            _finding("Keep", 0.91, "/tmp/a.py", 10, 12, priority=1),
            _finding("Drop", 0.95, "/tmp/b.py", 4, 4, priority=None),
        ),
        overall_correctness="incorrect",
        overall_explanation="There is one prioritized issue.",
        overall_confidence_score=0.84,
    )

    accepted = accepted_review_findings(review_output)

    assert [finding.title for finding in accepted] == ["Keep"]


def test_ai_review_feedback_filters_lower_priority_findings() -> None:
    review_output = ReviewOutput(
        findings=(
            _finding("Keep", 0.91, "/tmp/a.py", 10, 12, priority=1),
            _finding("Drop", 0.95, "/tmp/b.py", 4, 4, priority=2),
        ),
        overall_correctness="incorrect",
        overall_explanation="There is one urgent issue.",
        overall_confidence_score=0.84,
    )

    accepted = accepted_review_findings(review_output)

    assert [finding.title for finding in accepted] == ["Keep"]


def test_ai_review_feedback_handles_missing_priority() -> None:
    feedback = ai_review_feedback_prompt(
        findings=(_finding("Untitled", 0.91, "/tmp/a.py", 10, 12, priority=None),),
        review_types=(
            WorkflowReviewType(review_name="Security", prompt_ref="security"),
        ),
        attempt=1,
        max_attempts=3,
    )

    assert "- Untitled" in feedback


def test_ai_review_prompt_rejects_unexpected_vendored_prompt_shape(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "code_factory.workspace.ai_review.ai_review_prompt.base_review_prompt",
        lambda: "missing anchor",
    )

    try:
        render_ai_review_prompt(
            Issue(identifier="BEN-28"),
            WorkflowReviewType(review_name="Security", prompt_ref="security"),
            "Look for security regressions first.",
            review_scope="worktree",
            base_ref=None,
            changed_paths=(),
            lines_changed=0,
        )
    except RuntimeError as exc:
        assert str(exc) == "vendored_review_prompt_missing_detail_guidelines_anchor"
    else:  # pragma: no cover - defensive assertion for the expected exception path
        raise AssertionError("expected prompt drift to raise RuntimeError")


def _finding(
    title: str,
    confidence: float,
    path: str,
    start: int,
    end: int,
    *,
    priority: int | None = 1,
) -> ReviewFinding:
    return ReviewFinding(
        title=title,
        body=f"{title} details",
        code_location=ReviewCodeLocation(
            absolute_file_path=path,
            line_range=ReviewLineRange(start=start, end=end),
        ),
        confidence_score=confidence,
        priority=priority,
    )
