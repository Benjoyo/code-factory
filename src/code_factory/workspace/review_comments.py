"""PR review comment helpers for the operator TUI."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from ..errors import ReviewError
from .review_shell import capture_shell


@dataclass(frozen=True, slots=True)
class SubmittedReviewComment:
    kind: str
    body: str
    pr_url: str

    @property
    def label(self) -> str:
        first_line = self.body.splitlines()[0] if self.body else ""
        preview = " ".join(first_line.split())
        return f"{self.kind}: {preview}" if preview else f"{self.kind}:"


def review_comment_body(kind: str, body: str) -> str:
    return f"## {kind}\n\n{body.strip()}"


async def submit_review_comment(
    repo_root: str,
    *,
    pr_number: int,
    pr_url: str,
    kind: str,
    body: str,
) -> SubmittedReviewComment:
    text = body.strip()
    if not text:
        raise ReviewError("Review comment text can't be blank.")
    markdown = review_comment_body(kind, text)
    command = f"gh pr comment {pr_number} --body {shlex.quote(markdown)}"
    result = await capture_shell(command, cwd=repo_root)
    if result.status != 0:
        raise ReviewError(
            result.output or f"Failed to submit PR comment for #{pr_number}."
        )
    return SubmittedReviewComment(kind=kind, body=text, pr_url=pr_url)
