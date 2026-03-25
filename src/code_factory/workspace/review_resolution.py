"""Target resolution helpers for `cf review`."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast

from ..config.models import Settings
from ..errors import ReviewError
from ..trackers.base import Tracker, build_tracker
from .review_models import ReviewTarget
from .review_shell import ShellResult, capture_shell

_TRAILING_DIGITS_RE = re.compile(r"(\d+)$")


class ShellCapture(Protocol):
    def __call__(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> Awaitable[ShellResult]: ...


async def resolve_review_targets(
    repo_root: str,
    settings: Settings,
    targets: list[str],
    *,
    tracker_factory: Callable[..., Tracker] = build_tracker,
    shell_capture: ShellCapture = capture_shell,
) -> list[ReviewTarget]:
    normalized_targets = dedupe_review_targets(targets)
    tracker = tracker_factory(settings)
    try:
        if any(target.lower() != "main" for target in normalized_targets):
            await ensure_github_ready(repo_root, shell_capture=shell_capture)
        resolved: list[ReviewTarget] = []
        for target in normalized_targets:
            if target.lower() == "main":
                resolved.append(
                    ReviewTarget(
                        target="main",
                        kind="main",
                        ticket_identifier=None,
                        ticket_number=None,
                        ref=await resolve_main_ref(
                            repo_root, shell_capture=shell_capture
                        ),
                    )
                )
                continue
            resolved.append(
                await resolve_ticket_target(
                    tracker,
                    repo_root,
                    target,
                    shell_capture=shell_capture,
                )
            )
        return resolved
    finally:
        close = cast(
            Callable[[], Awaitable[object]] | None, getattr(tracker, "close", None)
        )
        if close is not None:
            await close()


async def resolve_repo_root(
    workflow_path: str,
    *,
    shell_capture: ShellCapture = capture_shell,
) -> str:
    workflow_dir = str(Path(workflow_path).resolve().parent)
    result = await _capture(
        "git rev-parse --show-toplevel",
        cwd=workflow_dir,
        shell_capture=shell_capture,
    )
    if result.status != 0 or not result.stdout.strip():
        raise ReviewError(
            f"Workflow root is not inside a git repository: {workflow_dir}"
        )
    return result.stdout.strip()


def dedupe_review_targets(targets: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw_target in targets:
        target = raw_target.strip()
        if not target:
            continue
        key = "main" if target.lower() == "main" else target
        if key in seen:
            continue
        seen.add(key)
        deduped.append("main" if target.lower() == "main" else target)
    return deduped


async def ensure_github_ready(
    repo_root: str,
    *,
    shell_capture: ShellCapture,
) -> None:
    if shutil.which("gh") is None:
        raise ReviewError(
            "GitHub CLI (`gh`) is required for `cf review` ticket targets."
        )
    result = await _capture(
        "gh auth status", cwd=repo_root, shell_capture=shell_capture
    )
    if result.status != 0:
        reason = result.output or "unknown authentication failure"
        raise ReviewError(f"`gh` is not authenticated: {reason}")


async def resolve_main_ref(
    repo_root: str,
    *,
    shell_capture: ShellCapture,
) -> str:
    fetch = await _capture(
        "git fetch origin",
        cwd=repo_root,
        shell_capture=shell_capture,
    )
    if fetch.status != 0:
        raise ReviewError(
            f"Failed to fetch origin before resolving main review target: {fetch.output or fetch.status}"
        )
    result = await _capture(
        "git symbolic-ref --quiet --short refs/remotes/origin/HEAD",
        cwd=repo_root,
        shell_capture=shell_capture,
    )
    if result.status != 0 or not result.stdout.strip():
        return "origin/main"
    return result.stdout.strip()


async def resolve_ticket_target(
    tracker: Tracker,
    repo_root: str,
    identifier: str,
    *,
    shell_capture: ShellCapture,
) -> ReviewTarget:
    issue = await tracker.fetch_issue_by_identifier(identifier)
    if issue is None:
        raise ReviewError(f"Ticket not found: {identifier}")
    if not issue.branch_name:
        raise ReviewError(f"{identifier} does not have tracker branch metadata.")
    pr_number, pr_url, head_ref_oid = await fetch_pull_request(
        repo_root,
        issue.branch_name,
        shell_capture=shell_capture,
    )
    ticket_number = trailing_ticket_number(identifier)
    return ReviewTarget(
        target=identifier,
        kind="ticket",
        ticket_identifier=identifier,
        ticket_number=ticket_number,
        ref=head_ref_oid,
        branch_name=issue.branch_name,
        pr_number=pr_number,
        pr_url=pr_url,
        head_sha=head_ref_oid,
    )


async def fetch_pull_request(
    repo_root: str,
    branch_name: str,
    *,
    shell_capture: ShellCapture,
) -> tuple[int, str, str]:
    command = (
        "gh pr list "
        f"--head {shlex.quote(branch_name)} --state open "
        "--json number,url,headRefOid --limit 2"
    )
    result = await _capture(command, cwd=repo_root, shell_capture=shell_capture)
    if result.status != 0:
        raise ReviewError(
            result.output or f"Failed to query pull requests for {branch_name}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ReviewError("GitHub CLI returned invalid PR JSON.") from exc
    if not isinstance(payload, list):
        raise ReviewError("GitHub CLI returned an invalid PR list payload.")
    if len(payload) == 0:
        raise ReviewError(f"No open PR found for branch {branch_name}.")
    if len(payload) > 1:
        raise ReviewError(f"Multiple open PRs found for branch {branch_name}.")
    pr = payload[0]
    if not isinstance(pr, dict):
        raise ReviewError("GitHub CLI returned an invalid PR object.")
    if not isinstance(pr.get("number"), int):
        raise ReviewError("GitHub CLI PR payload is missing `number`.")
    if not isinstance(pr.get("url"), str):
        raise ReviewError("GitHub CLI PR payload is missing `url`.")
    if not isinstance(pr.get("headRefOid"), str):
        raise ReviewError("GitHub CLI PR payload is missing `headRefOid`.")
    return pr["number"], pr["url"], pr["headRefOid"]


def trailing_ticket_number(identifier: str) -> int | None:
    match = _TRAILING_DIGITS_RE.search(identifier)
    if match is None:
        return None
    return int(match.group(1))


async def _capture(
    command: str,
    *,
    cwd: str,
    shell_capture: ShellCapture,
) -> ShellResult:
    result = await shell_capture(command, cwd=cwd)
    return result
