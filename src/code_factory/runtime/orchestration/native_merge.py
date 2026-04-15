"""Native PR merge fast path for landing states."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...issues import Issue
from ...structured_results import StructuredTurnResult
from ...trackers.base import Tracker
from ...workflow.models import WorkflowSnapshot
from ...workspace.review.review_resolution import ReviewError, ensure_github_ready
from ...workspace.review.review_shell import capture_shell
from ..worker.results import persist_state_result
from .native_merge_feedback import blocking_feedback_error

_CHECK_SUCCESS_CONCLUSIONS = {"success", "skipped", "neutral"}
_MERGEABLE_READY = {"MERGEABLE"}
_MERGE_STATE_BLOCKERS = {"BEHIND", "BLOCKED", "DIRTY", "DRAFT", "UNKNOWN"}
_MERGE_SUMMARY = "Approved implementation landed without additional merge fixes."
_MIN_TIME = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class NativeMergeAttemptResult:
    merged: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MergePullRequest:
    number: int
    url: str
    head_sha: str
    branch_name: str
    title: str
    body: str
    mergeable: str | None
    merge_state: str | None


async def attempt_native_merge(
    issue: Issue,
    workflow_snapshot: WorkflowSnapshot,
    tracker: Tracker,
    *,
    shell_capture=capture_shell,
) -> NativeMergeAttemptResult:
    repo_root = str(Path(workflow_snapshot.path).resolve().parent)
    if not issue.id:
        return NativeMergeAttemptResult(skip_reason="issue is missing an id")
    if not issue.branch_name:
        return NativeMergeAttemptResult(skip_reason="issue has no branch metadata")
    if not await _is_git_repository(repo_root, shell_capture=shell_capture):
        return NativeMergeAttemptResult(
            skip_reason="workflow repo root is not a git repository"
        )
    try:
        await ensure_github_ready(repo_root, shell_capture=shell_capture)
    except ReviewError as exc:
        return NativeMergeAttemptResult(skip_reason=str(exc))
    try:
        pr = await _fetch_pull_request(
            repo_root, issue.branch_name, shell_capture=shell_capture
        )
    except ReviewError as exc:
        return NativeMergeAttemptResult(skip_reason=str(exc))
    readiness_error = await _native_merge_readiness_error(
        repo_root,
        pr,
        shell_capture=shell_capture,
    )
    if readiness_error is not None:
        return NativeMergeAttemptResult(skip_reason=readiness_error)
    merge_result = await shell_capture(
        _merge_command(pr),
        cwd=repo_root,
    )
    if merge_result.status != 0:
        return NativeMergeAttemptResult(
            skip_reason=merge_result.output or "native merge command failed"
        )
    await persist_state_result(
        tracker,
        issue,
        issue.state or "Merging",
        StructuredTurnResult(
            decision="transition",
            summary=_MERGE_SUMMARY,
            next_state="Done",
        ),
    )
    await tracker.update_issue_state(issue.id, "Done")
    return NativeMergeAttemptResult(merged=True)


async def _native_merge_readiness_error(
    repo_root: str,
    pr: MergePullRequest,
    *,
    shell_capture,
) -> str | None:
    if pr.mergeable not in _MERGEABLE_READY:
        return f"PR mergeability is {pr.mergeable or 'unknown'}"
    if pr.merge_state in _MERGE_STATE_BLOCKERS or pr.merge_state is None:
        return f"PR merge state is {pr.merge_state or 'unknown'}"
    branch_head = await _fetch_pr_branch_head(
        repo_root,
        pr.number,
        shell_capture=shell_capture,
    )
    if branch_head != pr.head_sha:
        return "PR head does not match the latest branch head on GitHub"
    check_runs = await _get_check_runs(
        repo_root, pr.head_sha, shell_capture=shell_capture
    )
    if not check_runs:
        return "no check runs reported for the PR head"
    pending, failed, failures = _summarize_checks(check_runs)
    if pending:
        return "PR checks are still pending"
    if failed:
        return "PR checks are failing: " + ", ".join(failures)
    feedback_error = await _blocking_feedback_error(
        repo_root,
        pr.number,
        shell_capture=shell_capture,
    )
    return feedback_error


async def _fetch_pull_request(
    repo_root: str,
    branch_name: str,
    *,
    shell_capture,
) -> MergePullRequest:
    listing = await _capture_json(
        (
            "gh pr list "
            f"--head {shlex.quote(branch_name)} --state open "
            "--json number --limit 2"
        ),
        cwd=repo_root,
        shell_capture=shell_capture,
        error_prefix=f"Failed to query pull requests for {branch_name}",
    )
    if not isinstance(listing, list):
        raise ReviewError("GitHub CLI returned an invalid PR list payload.")
    if len(listing) == 0:
        raise ReviewError(f"No open PR found for branch {branch_name}.")
    if len(listing) > 1:
        raise ReviewError(f"Multiple open PRs found for branch {branch_name}.")
    pr_number = listing[0].get("number")
    details = await _capture_json(
        "gh pr view "
        f"{shlex.quote(str(pr_number))} "
        "--json number,url,headRefOid,headRefName,title,body,mergeable,mergeStateStatus",
        cwd=repo_root,
        shell_capture=shell_capture,
        error_prefix=f"Failed to load PR details for branch {branch_name}",
    )
    if not isinstance(details, dict):
        raise ReviewError("GitHub CLI returned an invalid PR payload.")
    return MergePullRequest(
        number=_require_int(details, "number"),
        url=_require_str(details, "url"),
        head_sha=_require_str(details, "headRefOid"),
        branch_name=_require_str(details, "headRefName"),
        title=_require_str(details, "title"),
        body=_optional_str(details, "body") or "",
        mergeable=_optional_str(details, "mergeable"),
        merge_state=_optional_str(details, "mergeStateStatus"),
    )


async def _fetch_pr_branch_head(
    repo_root: str,
    pr_number: int,
    *,
    shell_capture,
) -> str:
    payload = await _capture_json(
        f"gh api repos/{{owner}}/{{repo}}/pulls/{pr_number}",
        cwd=repo_root,
        shell_capture=shell_capture,
        error_prefix=f"Failed to query PR head for {pr_number}",
    )
    head = payload.get("head") if isinstance(payload, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not sha.strip():
        raise ReviewError("GitHub CLI PR payload is missing `head.sha`.")
    return sha


async def _blocking_feedback_error(
    repo_root: str,
    pr_number: int,
    *,
    shell_capture,
) -> str | None:
    issue_comments = await _get_paginated_list(
        repo_root,
        f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
        shell_capture=shell_capture,
    )
    review_comments = await _get_paginated_list(
        repo_root,
        f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments",
        shell_capture=shell_capture,
    )
    reviews = await _get_paginated_list(
        repo_root,
        f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews",
        shell_capture=shell_capture,
    )
    return blocking_feedback_error(issue_comments, review_comments, reviews)


async def _get_check_runs(
    repo_root: str, head_sha: str, *, shell_capture
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = await _capture_json(
            (
                f"gh api repos/{{owner}}/{{repo}}/commits/{head_sha}/check-runs "
                f"-f per_page=100 -f page={page}"
            ),
            cwd=repo_root,
            shell_capture=shell_capture,
            error_prefix=f"Failed to query check runs for {head_sha}",
        )
        batch = payload.get("check_runs", []) if isinstance(payload, dict) else []
        if not batch:
            return runs
        runs.extend([item for item in batch if isinstance(item, dict)])
        total_count = payload.get("total_count") if isinstance(payload, dict) else None
        if isinstance(total_count, int) and len(runs) >= total_count:
            return runs
        page += 1


async def _get_paginated_list(
    repo_root: str, endpoint: str, *, shell_capture
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = await _capture_json(
            f"gh api --method GET {endpoint} -f per_page=100 -f page={page}",
            cwd=repo_root,
            shell_capture=shell_capture,
            error_prefix=f"Failed to query {endpoint}",
        )
        if not isinstance(batch, list) or not batch:
            return items
        items.extend([item for item in batch if isinstance(item, dict)])
        page += 1


async def _is_git_repository(repo_root: str, *, shell_capture) -> bool:
    result = await shell_capture("git rev-parse --is-inside-work-tree", cwd=repo_root)
    return result.status == 0 and result.stdout.strip() == "true"


async def _capture_json(
    command: str, *, cwd: str, shell_capture, error_prefix: str
) -> Any:
    result = await shell_capture(command, cwd=cwd)
    if result.status != 0:
        raise ReviewError(result.output or error_prefix)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise ReviewError(f"{error_prefix}: invalid JSON") from exc


def _merge_command(pr: MergePullRequest) -> str:
    return (
        "gh pr merge "
        f"{shlex.quote(str(pr.number))} "
        "--squash --delete-branch "
        f"--subject {shlex.quote(pr.title)} "
        f"--body {shlex.quote(pr.body)}"
    )


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    raise ReviewError(f"GitHub CLI PR payload is missing `{key}`.")


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    raise ReviewError(f"GitHub CLI PR payload is missing `{key}`.")


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _summarize_checks(check_runs: list[dict[str, Any]]) -> tuple[bool, bool, list[str]]:
    pending = False
    failures: list[str] = []
    for check in _dedupe_check_runs(check_runs):
        if check.get("status") != "completed":
            pending = True
            continue
        conclusion = check.get("conclusion")
        if conclusion not in _CHECK_SUCCESS_CONCLUSIONS:
            failures.append(f"{check.get('name', 'unknown')}: {conclusion}")
    return pending, bool(failures), failures


def _dedupe_check_runs(check_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_name: dict[str, dict[str, Any]] = {}
    for check in check_runs:
        name = str(check.get("name") or "unknown")
        current = latest_by_name.get(name)
        if current is None or _check_timestamp(check) > _check_timestamp(current):
            latest_by_name[name] = check
    return list(latest_by_name.values())


def _check_timestamp(check: dict[str, Any]) -> datetime:
    for key in ("completed_at", "started_at", "run_started_at", "created_at"):
        value = check.get(key)
        if isinstance(value, str):
            return _parse_time(value)
    return _MIN_TIME


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
