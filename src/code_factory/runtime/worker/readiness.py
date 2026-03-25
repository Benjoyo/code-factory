"""Native completion readiness checks for transition proposals."""

from __future__ import annotations

from ...issues import Issue
from ...workflow.models import WorkflowStateProfile
from ...workspace.hooks import HookCommandResult
from ...workspace.repository import (
    current_branch_name,
    ensure_git_repository,
    head_sha,
    upstream_head_sha,
    upstream_name,
    worktree_status,
)
from ...workspace.review_resolution import (
    ReviewError,
    ensure_github_ready,
    fetch_pull_request,
)


async def native_readiness_result(
    workspace: str,
    issue: Issue,
    profile: WorkflowStateProfile,
) -> HookCommandResult | None:
    """Run the configured native readiness checks for one completion attempt."""

    completion = profile.completion
    if not completion.enabled:
        return None
    try:
        await ensure_git_repository(workspace)
    except Exception:
        return _blocked("Completion blocked: the workspace is not a git repository.")
    branch = await current_branch_name(workspace)
    if branch is None:
        return _blocked(
            "Completion blocked: HEAD is detached. Check out the issue branch and retry."
        )
    if issue.branch_name and branch != issue.branch_name:
        return _blocked(
            "Completion blocked: current branch "
            f"`{branch}` does not match tracker branch `{issue.branch_name}`."
        )
    status = await worktree_status(workspace)
    if status:
        return _blocked(
            f"Completion blocked: the worktree is dirty.\n\n```text\n{status}\n```"
        )
    upstream = await upstream_name(workspace)
    if upstream is None:
        return _blocked(
            "Completion blocked: the current branch has no upstream. Push with "
            "`git push -u origin HEAD` and retry."
        )
    local_head = await head_sha(workspace)
    remote_head = await upstream_head_sha(workspace)
    if remote_head is None:
        return _blocked(
            "Completion blocked: the configured branch upstream could not be "
            "resolved on remote. Push or fetch the issue branch and retry."
        )
    if local_head != remote_head:
        return _blocked(
            "Completion blocked: local HEAD is not fully pushed to the branch upstream."
        )
    if not completion.require_pr:
        return _passed(local_head)
    try:
        await ensure_github_ready(workspace, shell_capture=_capture)
        _number, url, pr_head = await fetch_pull_request(
            workspace,
            branch,
            shell_capture=_capture,
        )
    except ReviewError as exc:
        return _blocked(f"Completion blocked: {exc}")
    if pr_head != local_head:
        return _blocked(
            "Completion blocked: the open PR head does not match local HEAD."
        )
    return _passed(f"{local_head}\n{url}")


def _blocked(message: str) -> HookCommandResult:
    return HookCommandResult(status=2, stdout="", stderr=message)


def _passed(stdout: str) -> HookCommandResult:
    return HookCommandResult(status=0, stdout=stdout, stderr="")


async def _capture(command: str, *, cwd: str, env: dict[str, str] | None = None):
    from ...workspace.review_shell import capture_shell

    return await capture_shell(command, cwd=cwd, env=env)
