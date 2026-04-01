from __future__ import annotations

import asyncio

import pytest

from code_factory.errors import ReviewError
from code_factory.workspace.review.review_comments import (
    SubmittedReviewComment,
    review_comment_body,
    submit_review_comment,
)
from code_factory.workspace.review.review_shell import ShellResult


def test_review_comment_body_and_label() -> None:
    assert review_comment_body("Bug", "broken flow") == "## Bug\n\nbroken flow"
    submitted = SubmittedReviewComment(
        kind="Change",
        body="update spacing\nand text",
        pr_url="https://example/pr/1",
    )
    assert submitted.label == "Change: update spacing"


@pytest.mark.asyncio
async def test_submit_review_comment_success_and_failure(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_capture(command: str, *, cwd: str, env=None) -> ShellResult:
        calls.append((command, cwd))
        return ShellResult(0, "", "")

    monkeypatch.setattr(
        "code_factory.workspace.review.review_comments.capture_shell", fake_capture
    )
    submitted = await submit_review_comment(
        "/repo",
        pr_number=12,
        pr_url="https://example/pr/12",
        kind="Bug",
        body="broken flow",
    )
    assert submitted.kind == "Bug"
    assert submitted.pr_url == "https://example/pr/12"
    assert "gh pr comment 12 --body" in calls[0][0]
    assert "/repo" == calls[0][1]

    with pytest.raises(ReviewError, match="can't be blank"):
        await submit_review_comment(
            "/repo",
            pr_number=12,
            pr_url="https://example/pr/12",
            kind="Bug",
            body="   ",
        )

    monkeypatch.setattr(
        "code_factory.workspace.review.review_comments.capture_shell",
        lambda command, *, cwd, env=None: asyncio.sleep(
            0, result=ShellResult(1, "", "gh failed")
        ),
    )
    with pytest.raises(ReviewError, match="gh failed"):
        await submit_review_comment(
            "/repo",
            pr_number=12,
            pr_url="https://example/pr/12",
            kind="Change",
            body="needs tweak",
        )
