"""AI review trigger evaluation against the current workspace diff."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ...errors import ReviewError
from ...workflow.profiles.review_profiles import (
    AI_REVIEW_SCOPE_BRANCH,
    AI_REVIEW_SCOPE_WORKTREE,
    ResolvedAiReviewScope,
    WorkflowReviewType,
)
from ..repository import (
    default_base_ref,
    ensure_git_repository,
    repository_command,
    run_repository_command,
    worktree_status,
)

_EMPTY_TREE_REF = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_SHORTSTAT_COUNT = re.compile(r"(\d+)\s+(?:insertions?|deletions?)\([+-]\)")
_DIRTY_STATUS_LIMIT = 2_000


@dataclass(frozen=True, slots=True)
class WorktreeReviewSurface:
    """The current candidate patch viewed as changed paths plus changed lines."""

    changed_paths: tuple[str, ...]
    lines_changed: int
    review_scope: ResolvedAiReviewScope = AI_REVIEW_SCOPE_WORKTREE
    base_ref: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_paths)


@dataclass(frozen=True, slots=True)
class WorktreeReviewSelection:
    """Review trigger result ready for runtime slices to consume."""

    surface: WorktreeReviewSurface
    matched_types: tuple[WorkflowReviewType, ...]


async def collect_worktree_review_surface(workspace: str) -> WorktreeReviewSurface:
    """Inspect the current git worktree diff and derive review trigger inputs."""

    return await collect_worktree_review_surface_for_scope(workspace)


async def collect_worktree_review_surface_for_scope(
    workspace: str,
    *,
    review_scope: ResolvedAiReviewScope = AI_REVIEW_SCOPE_WORKTREE,
) -> WorktreeReviewSurface:
    """Inspect the requested review surface and derive trigger inputs."""

    await ensure_git_repository(workspace)
    if review_scope == AI_REVIEW_SCOPE_BRANCH:
        return await _collect_branch_review_surface(workspace)
    return await _collect_worktree_review_surface(workspace)


async def select_worktree_review_types(
    workspace: str,
    review_types: Sequence[WorkflowReviewType],
    *,
    review_scope: ResolvedAiReviewScope = AI_REVIEW_SCOPE_WORKTREE,
) -> WorktreeReviewSelection:
    """Return the current review surface plus the review types it triggers."""

    surface = await collect_worktree_review_surface_for_scope(
        workspace, review_scope=review_scope
    )
    return WorktreeReviewSelection(
        surface=surface,
        matched_types=match_worktree_review_types(review_types, surface),
    )


def match_worktree_review_types(
    review_types: Sequence[WorkflowReviewType],
    surface: WorktreeReviewSurface,
) -> tuple[WorkflowReviewType, ...]:
    """Filter workflow review types against one current-worktree review surface."""

    return tuple(
        review_type
        for review_type in review_types
        if review_type_matches_surface(review_type, surface)
    )


def review_type_matches_surface(
    review_type: WorkflowReviewType,
    surface: WorktreeReviewSurface,
) -> bool:
    """Return whether one review type should run for the provided diff surface."""

    if not surface.has_changes:
        return False
    if (
        review_type.lines_changed is not None
        and surface.lines_changed < review_type.lines_changed
    ):
        return False
    changed_paths = surface.changed_paths
    if review_type.paths.only and not all(
        _matches_any(path, review_type.paths.only) for path in changed_paths
    ):
        return False
    if review_type.paths.include and not any(
        _matches_any(path, review_type.paths.include) for path in changed_paths
    ):
        return False
    if review_type.paths.exclude and any(
        _matches_any(path, review_type.paths.exclude) for path in changed_paths
    ):
        return False
    return True


async def _diff_base_ref(workspace: str) -> str:
    result = await repository_command(
        workspace,
        "git rev-parse --verify HEAD",
    )
    ref = result.stdout.strip()
    return ref if result.status == 0 and ref else _EMPTY_TREE_REF


async def _collect_worktree_review_surface(workspace: str) -> WorktreeReviewSurface:
    diff_base = await _diff_base_ref(workspace)
    tracked_paths = await _tracked_changed_paths(workspace, diff_base)
    untracked_paths = await _untracked_paths(workspace)
    changed_paths = tuple(sorted({*tracked_paths, *untracked_paths}))
    return WorktreeReviewSurface(
        changed_paths=changed_paths,
        lines_changed=await _tracked_lines_changed(workspace, diff_base)
        + sum(
            _count_untracked_lines(Path(workspace), path) for path in untracked_paths
        ),
        review_scope=AI_REVIEW_SCOPE_WORKTREE,
    )


async def _collect_branch_review_surface(workspace: str) -> WorktreeReviewSurface:
    head = await repository_command(workspace, "git rev-parse --verify HEAD")
    if head.status != 0 or not head.stdout.strip():
        raise ReviewError(
            "AI review with branch scope requires a committed HEAD in the workspace."
        )
    status = await worktree_status(workspace)
    if status:
        raise ReviewError(_branch_scope_dirty_message(status))
    base_ref = await default_base_ref(workspace)
    base = await repository_command(
        workspace, f"git rev-parse --verify {shlex.quote(base_ref)}"
    )
    if base.status != 0 or not base.stdout.strip():
        raise ReviewError(
            f"AI review with branch scope could not resolve the default base ref `{base_ref}`."
        )
    merge_base = await repository_command(
        workspace, f"git merge-base HEAD {shlex.quote(base_ref)}"
    )
    merge_base_ref = merge_base.stdout.strip()
    if merge_base.status != 0 or not merge_base_ref:
        raise ReviewError(
            f"AI review with branch scope could not determine a merge-base between HEAD and `{base_ref}`."
        )
    return WorktreeReviewSurface(
        changed_paths=await _tracked_changed_paths_between(
            workspace, merge_base_ref, "HEAD"
        ),
        lines_changed=await _tracked_lines_changed_between(
            workspace, merge_base_ref, "HEAD"
        ),
        review_scope=AI_REVIEW_SCOPE_BRANCH,
        base_ref=base_ref,
    )


async def _tracked_changed_paths(workspace: str, diff_base: str) -> tuple[str, ...]:
    return await _tracked_changed_paths_between(workspace, diff_base)


async def _tracked_changed_paths_between(
    workspace: str,
    diff_base: str,
    diff_head: str | None = None,
) -> tuple[str, ...]:
    diff_target = _diff_target(diff_base, diff_head)
    result = await run_repository_command(
        workspace,
        f"git diff --name-only -z --find-renames {diff_target} --",
    )
    return _nul_separated_paths(result.stdout)


async def _untracked_paths(workspace: str) -> tuple[str, ...]:
    result = await run_repository_command(
        workspace,
        "git ls-files -z --others --exclude-standard --",
    )
    return _nul_separated_paths(result.stdout)


async def _tracked_lines_changed(workspace: str, diff_base: str) -> int:
    return await _tracked_lines_changed_between(workspace, diff_base)


async def _tracked_lines_changed_between(
    workspace: str,
    diff_base: str,
    diff_head: str | None = None,
) -> int:
    diff_target = _diff_target(diff_base, diff_head)
    result = await run_repository_command(
        workspace,
        f"git diff --shortstat --find-renames {diff_target} --",
    )
    return _parse_shortstat_lines(result.stdout)


def _nul_separated_paths(stdout: str) -> tuple[str, ...]:
    return tuple(path for path in stdout.split("\0") if path)


def _parse_shortstat_lines(stdout: str) -> int:
    total = 0
    for match in _SHORTSTAT_COUNT.finditer(stdout):
        total += int(match.group(1))
    return total


def _count_untracked_lines(workspace_root: Path, relative_path: str) -> int:
    try:
        contents = (workspace_root / relative_path).read_bytes()
    except OSError:
        return 0
    if b"\0" in contents:
        return 0
    return len(contents.splitlines())


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_path_matches_pattern(path, pattern) for pattern in patterns)


def _diff_target(diff_base: str, diff_head: str | None) -> str:
    if diff_head is None:
        return shlex.quote(diff_base)
    return f"{shlex.quote(diff_base)}..{shlex.quote(diff_head)}"


def _branch_scope_dirty_message(status: str) -> str:
    summarized = status
    if len(summarized) > _DIRTY_STATUS_LIMIT:
        summarized = (
            f"{summarized[:_DIRTY_STATUS_LIMIT]}\n\n"
            "[truncated: git status output exceeded 2000 characters]"
        )
    return (
        "AI review with branch scope requires a clean worktree. "
        "Commit, stash, or discard local changes and retry.\n\n"
        "Current git status --short:\n"
        f"```text\n{summarized}\n```"
    )


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        prefix = pattern.removesuffix("/**").rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    return PurePosixPath(path).match(pattern)
