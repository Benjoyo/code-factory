"""AI review trigger evaluation against the current workspace diff."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..workflow.review_profiles import WorkflowReviewType
from .repository import (
    ensure_git_repository,
    repository_command,
    run_repository_command,
)

_EMPTY_TREE_REF = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_SHORTSTAT_COUNT = re.compile(r"(\d+)\s+(?:insertions?|deletions?)\([+-]\)")


@dataclass(frozen=True, slots=True)
class WorktreeReviewSurface:
    """The current candidate patch viewed as changed paths plus changed lines."""

    changed_paths: tuple[str, ...]
    lines_changed: int

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

    await ensure_git_repository(workspace)
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
    )


async def select_worktree_review_types(
    workspace: str,
    review_types: Sequence[WorkflowReviewType],
) -> WorktreeReviewSelection:
    """Return the current review surface plus the review types it triggers."""

    surface = await collect_worktree_review_surface(workspace)
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


async def _tracked_changed_paths(workspace: str, diff_base: str) -> tuple[str, ...]:
    result = await run_repository_command(
        workspace,
        f"git diff --name-only -z --find-renames {shlex.quote(diff_base)} --",
    )
    return _nul_separated_paths(result.stdout)


async def _untracked_paths(workspace: str) -> tuple[str, ...]:
    result = await run_repository_command(
        workspace,
        "git ls-files -z --others --exclude-standard --",
    )
    return _nul_separated_paths(result.stdout)


async def _tracked_lines_changed(workspace: str, diff_base: str) -> int:
    result = await run_repository_command(
        workspace,
        f"git diff --shortstat --find-renames {shlex.quote(diff_base)} --",
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


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        prefix = pattern.removesuffix("/**").rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    return PurePosixPath(path).match(pattern)
