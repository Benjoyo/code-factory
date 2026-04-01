from __future__ import annotations

from dataclasses import dataclass

from ...runtime.subprocess import ProcessTree


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    target: str
    kind: str
    ticket_identifier: str | None
    ticket_number: int | None
    ref: str
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    head_sha: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewLaunch:
    name: str
    command: str
    port: int | None
    url: str | None
    open_browser: bool


@dataclass(slots=True)
class RunningReviewServer:
    target: ReviewTarget
    launch: ReviewLaunch
    worktree: str
    process: ProcessTree
    head_sha: str
