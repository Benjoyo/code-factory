from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_factory.workflow.review_profiles import ReviewPathTriggers, WorkflowReviewType
from code_factory.workspace.review_surface import (
    WorktreeReviewSelection,
    WorktreeReviewSurface,
    _count_untracked_lines,
    _parse_shortstat_lines,
    _path_matches_pattern,
    collect_worktree_review_surface,
    match_worktree_review_types,
    review_type_matches_surface,
    select_worktree_review_types,
)


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    run_git(path, "init")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.com")


def review_type(
    name: str,
    *,
    lines_changed: int | None = None,
    only: tuple[str, ...] = (),
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> WorkflowReviewType:
    return WorkflowReviewType(
        review_name=name,
        prompt_ref=name.lower(),
        lines_changed=lines_changed,
        paths=ReviewPathTriggers(only=only, include=include, exclude=exclude),
    )


def test_review_type_matches_surface_semantics() -> None:
    empty = WorktreeReviewSurface(changed_paths=(), lines_changed=0)
    assert review_type_matches_surface(review_type("General"), empty) is False

    mixed_surface = WorktreeReviewSurface(
        changed_paths=("src/api/service.py", "ui/app.tsx"),
        lines_changed=5,
    )
    assert review_type_matches_surface(review_type("General"), mixed_surface) is True
    assert (
        review_type_matches_surface(
            review_type("Frontend", only=("ui/**",)),
            mixed_surface,
        )
        is False
    )
    assert (
        review_type_matches_surface(
            review_type("Security", include=("src/**",), exclude=("tests/**",)),
            mixed_surface,
        )
        is True
    )
    assert (
        review_type_matches_surface(
            review_type("No Frontend", include=("web/**",)),
            mixed_surface,
        )
        is False
    )
    assert (
        review_type_matches_surface(
            review_type("Large Change", lines_changed=6),
            mixed_surface,
        )
        is False
    )
    assert (
        review_type_matches_surface(
            review_type("Skip Tests", exclude=("ui/**",)),
            mixed_surface,
        )
        is False
    )

    matched = match_worktree_review_types(
        (
            review_type("General"),
            review_type("Frontend", only=("ui/**",)),
            review_type("Security", include=("src/**",), exclude=("tests/**",)),
        ),
        mixed_surface,
    )
    assert tuple(review.review_name for review in matched) == ("General", "Security")


def test_review_surface_helpers_cover_edge_cases(tmp_path: Path) -> None:
    assert _parse_shortstat_lines("") == 0
    assert _parse_shortstat_lines(" 1 file changed, 2 insertions(+)\n") == 2
    assert _parse_shortstat_lines(" 1 file changed, 3 deletions(-)\n") == 3
    assert (
        _parse_shortstat_lines(" 2 files changed, 4 insertions(+), 5 deletions(-)\n")
        == 9
    )

    text_path = tmp_path / "note.txt"
    text_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert _count_untracked_lines(tmp_path, "note.txt") == 3

    binary_path = tmp_path / "blob.bin"
    binary_path.write_bytes(b"\0binary")
    assert _count_untracked_lines(tmp_path, "blob.bin") == 0
    assert _count_untracked_lines(tmp_path, "missing.txt") == 0

    assert _path_matches_pattern("src/app.py", "**") is True
    assert _path_matches_pattern("src/nested/app.py", "src/**") is True
    assert _path_matches_pattern("src/nested/app.py", "src/**/*.py") is True
    assert _path_matches_pattern("tests/app.py", "src/**") is False


@pytest.mark.asyncio
async def test_collect_worktree_review_surface_from_git_diff(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    (tmp_path / "tests" / "app_test.py").write_text("assert True\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "initial")

    (tmp_path / "src" / "app.py").write_text(
        "print('one')\nprint('two')\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "app_test.py").unlink()
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "new.ts").write_text("a\nb\nc\n", encoding="utf-8")

    surface = await collect_worktree_review_surface(str(tmp_path))
    assert surface == WorktreeReviewSurface(
        changed_paths=("src/app.py", "tests/app_test.py", "ui/new.ts"),
        lines_changed=5,
    )


@pytest.mark.asyncio
async def test_select_worktree_review_types_handles_repo_without_head(
    tmp_path: Path,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.tsx").write_text("one\ntwo\n", encoding="utf-8")

    selection = await select_worktree_review_types(
        str(tmp_path),
        (
            review_type("General"),
            review_type("Frontend", only=("web/**",), lines_changed=2),
            review_type("Large Change", lines_changed=3),
        ),
    )

    assert selection == WorktreeReviewSelection(
        surface=WorktreeReviewSurface(
            changed_paths=("web/index.tsx",),
            lines_changed=2,
        ),
        matched_types=(
            review_type("General"),
            review_type("Frontend", only=("web/**",), lines_changed=2),
        ),
    )
