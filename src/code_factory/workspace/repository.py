"""Repository helpers for workspace preparation and validation."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from ..errors import WorkspaceError
from ..issues import Issue
from .review_shell import ShellResult, capture_shell
from .workpad import WORKPAD_FILENAME

_BRANCH_COMPONENT_RE = re.compile(r"[^a-z0-9._/-]+")
_DIRTY_STATUS_LIMIT = 2_000


async def prepare_workspace_repository(workspace: str, issue: Issue) -> None:
    """Ensure the workspace is on the expected branch and ignores the local workpad."""

    await ensure_git_repository(workspace)
    await ensure_local_workpad_artifact(workspace)
    desired_branch = issue_branch_name(issue)
    current_branch = await current_branch_name(workspace)
    if current_branch == desired_branch:
        return
    dirty_status = await worktree_status(workspace)
    if dirty_status:
        raise WorkspaceError(
            (
                "workspace_branch_checkout_dirty",
                desired_branch,
                _summarize_status(dirty_status),
            )
        )
    if await branch_exists(workspace, desired_branch, remote=False):
        await run_repository_command(
            workspace,
            f"git checkout {shlex.quote(desired_branch)}",
        )
        return
    if await branch_exists(workspace, desired_branch, remote=True):
        await run_repository_command(
            workspace,
            "git checkout -b "
            f"{shlex.quote(desired_branch)} --track "
            f"{shlex.quote(f'origin/{desired_branch}')}",
        )
        return
    await run_repository_command(
        workspace,
        "git checkout -b "
        f"{shlex.quote(desired_branch)} "
        f"{shlex.quote(await default_base_ref(workspace))}",
    )


def issue_branch_name(issue: Issue) -> str:
    """Return the preferred branch name for the issue workspace."""

    if issue.branch_name:
        return issue.branch_name
    identifier = (issue.identifier or issue.id or "issue").strip().lower()
    normalized = _BRANCH_COMPONENT_RE.sub("-", identifier).strip("-/")
    return f"codex/{normalized or 'issue'}"


async def ensure_git_repository(workspace: str) -> None:
    """Raise when the workspace does not contain a git worktree."""

    result = await repository_command(workspace, "git rev-parse --is-inside-work-tree")
    if result.status == 0 and result.stdout.strip() == "true":
        return
    raise WorkspaceError(("workspace_repository_missing", workspace))


async def ensure_local_workpad_artifact(workspace: str) -> None:
    """Guard and ignore the orchestrator-owned local workpad file."""

    tracked = await repository_command(
        workspace,
        f"git ls-files --error-unmatch -- {shlex.quote(WORKPAD_FILENAME)}",
    )
    if tracked.status == 0:
        raise WorkspaceError(("workspace_workpad_tracked", WORKPAD_FILENAME))
    git_path = await run_repository_command(
        workspace, "git rev-parse --git-path info/exclude"
    )
    resolved_git_path = git_path.stdout.strip()
    exclude_path = Path(resolved_git_path)
    if not exclude_path.is_absolute():
        exclude_path = Path(workspace) / resolved_git_path
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"/{WORKPAD_FILENAME}"
    existing_lines = (
        exclude_path.read_text(encoding="utf-8").splitlines()
        if exclude_path.exists()
        else []
    )
    if entry in existing_lines:
        return
    existing_text = (
        exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    )
    if existing_text and not existing_text.endswith("\n"):
        existing_text += "\n"
    exclude_path.write_text(f"{existing_text}{entry}\n", encoding="utf-8")


async def current_branch_name(workspace: str) -> str | None:
    """Return the current branch name, or None when HEAD is detached."""

    result = await repository_command(
        workspace, "git symbolic-ref --quiet --short HEAD"
    )
    branch = result.stdout.strip()
    return branch if result.status == 0 and branch else None


async def worktree_status(workspace: str) -> str:
    """Return porcelain status output for the workspace."""

    result = await run_repository_command(
        workspace, "git status --porcelain=v1 --untracked-files=all"
    )
    return result.stdout.rstrip()


async def upstream_name(workspace: str) -> str | None:
    """Return the current branch upstream, or None when unset."""

    result = await repository_command(
        workspace, "git rev-parse --abbrev-ref --symbolic-full-name @{upstream}"
    )
    upstream = result.stdout.strip()
    return upstream if result.status == 0 and upstream else None


async def head_sha(workspace: str, ref: str = "HEAD") -> str:
    """Return the commit SHA for one ref."""

    result = await run_repository_command(
        workspace,
        f"git rev-parse {shlex.quote(ref)}",
    )
    return result.stdout.strip()


async def default_base_ref(workspace: str) -> str:
    """Resolve the default remote branch ref for new branches."""

    result = await repository_command(
        workspace, "git symbolic-ref --quiet --short refs/remotes/origin/HEAD"
    )
    ref = result.stdout.strip()
    return ref if result.status == 0 and ref else "origin/main"


async def branch_exists(workspace: str, branch: str, *, remote: bool) -> bool:
    """Return True when the requested local or remote branch already exists."""

    prefix = "refs/remotes/origin/" if remote else "refs/heads/"
    result = await repository_command(
        workspace,
        f"git show-ref --verify --quiet {shlex.quote(prefix + branch)}",
    )
    return result.status == 0


async def repository_command(workspace: str, command: str) -> ShellResult:
    """Run one repository command and capture the result."""

    return await capture_shell(command, cwd=workspace)


async def run_repository_command(workspace: str, command: str) -> ShellResult:
    """Run one repository command and raise a workspace error on failure."""

    result = await repository_command(workspace, command)
    if result.status == 0:
        return result
    raise WorkspaceError(
        ("workspace_repository_command_failed", command, result.output)
    )


def _summarize_status(status: str) -> str:
    if len(status) <= _DIRTY_STATUS_LIMIT:
        return status
    return (
        f"{status[:_DIRTY_STATUS_LIMIT]}\n\n"
        "[truncated: git status output exceeded 2000 characters]"
    )
