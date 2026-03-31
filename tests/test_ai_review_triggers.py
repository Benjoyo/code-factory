from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_factory.errors import ReviewError
from code_factory.workflow.review_profiles import ReviewPathTriggers, WorkflowReviewType
from code_factory.workspace.review_surface import (
    WorktreeReviewSelection,
    WorktreeReviewSurface,
    _branch_scope_dirty_message,
    _count_untracked_lines,
    _parse_shortstat_lines,
    _path_matches_pattern,
    collect_worktree_review_surface,
    collect_worktree_review_surface_for_scope,
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


def configure_origin_main(path: Path, ref: str) -> None:
    run_git(path, "update-ref", "refs/remotes/origin/main", ref)
    run_git(
        path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
    )


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

    oversized_status = "M " + ("a" * 2_100)
    assert "[truncated: git status output exceeded 2000 characters]" in (
        _branch_scope_dirty_message(oversized_status)
    )


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


@pytest.mark.asyncio
async def test_collect_branch_review_surface_uses_merge_base_to_head(
    tmp_path: Path,
) -> None:
    init_repo(tmp_path)
    run_git(tmp_path, "branch", "-m", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("one\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "initial")
    configure_origin_main(tmp_path, run_git(tmp_path, "rev-parse", "HEAD"))

    run_git(tmp_path, "checkout", "-b", "codex/eng-1")
    (tmp_path / "src" / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "second")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "app.tsx").write_text("a\nb\nc\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "third")

    surface = await collect_worktree_review_surface_for_scope(
        str(tmp_path), review_scope="branch"
    )

    assert surface == WorktreeReviewSurface(
        changed_paths=("src/app.py", "ui/app.tsx"),
        lines_changed=4,
        review_scope="branch",
        base_ref="origin/main",
    )


@pytest.mark.asyncio
async def test_collect_branch_review_surface_blocks_dirty_worktree(
    tmp_path: Path,
) -> None:
    init_repo(tmp_path)
    run_git(tmp_path, "branch", "-m", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("one\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "initial")
    configure_origin_main(tmp_path, run_git(tmp_path, "rev-parse", "HEAD"))

    run_git(tmp_path, "checkout", "-b", "codex/eng-1")
    (tmp_path / "src" / "app.py").write_text("one\ntwo\n", encoding="utf-8")

    with pytest.raises(ReviewError, match="requires a clean worktree") as exc_info:
        await collect_worktree_review_surface_for_scope(
            str(tmp_path), review_scope="branch"
        )
    assert "Current git status --short:" in str(exc_info.value)
    assert " M src/app.py" in str(exc_info.value)


@pytest.mark.asyncio
async def test_collect_branch_review_surface_blocks_repo_without_head(
    tmp_path: Path,
) -> None:
    init_repo(tmp_path)

    with pytest.raises(ReviewError, match="requires a committed HEAD"):
        await collect_worktree_review_surface_for_scope(
            str(tmp_path), review_scope="branch"
        )


@pytest.mark.asyncio
async def test_collect_branch_review_surface_blocks_unresolved_default_base(
    tmp_path: Path,
) -> None:
    init_repo(tmp_path)
    run_git(tmp_path, "branch", "-m", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("one\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "initial")
    run_git(tmp_path, "checkout", "-b", "codex/eng-1")

    with pytest.raises(ReviewError, match="could not resolve the default base ref"):
        await collect_worktree_review_surface_for_scope(
            str(tmp_path), review_scope="branch"
        )


@pytest.mark.asyncio
async def test_collect_branch_review_surface_blocks_missing_merge_base(
    tmp_path: Path,
) -> None:
    init_repo(tmp_path)
    run_git(tmp_path, "branch", "-m", "main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("one\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "initial")

    run_git(tmp_path, "checkout", "--orphan", "unrelated")
    run_git(tmp_path, "commit", "--allow-empty", "-m", "unrelated")
    unrelated_sha = run_git(tmp_path, "rev-parse", "HEAD")
    run_git(tmp_path, "checkout", "main")
    configure_origin_main(tmp_path, unrelated_sha)

    with pytest.raises(ReviewError, match="could not determine a merge-base"):
        await collect_worktree_review_surface_for_scope(
            str(tmp_path), review_scope="branch"
        )


@pytest.mark.asyncio
async def test_select_branch_review_types_handles_no_committed_delta(
    tmp_path: Path,
) -> None:
    init_repo(tmp_path)
    run_git(tmp_path, "branch", "-m", "main")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.tsx").write_text("one\ntwo\n", encoding="utf-8")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-m", "initial")
    configure_origin_main(tmp_path, run_git(tmp_path, "rev-parse", "HEAD"))

    run_git(tmp_path, "checkout", "-b", "codex/eng-1")

    selection = await select_worktree_review_types(
        str(tmp_path),
        (
            review_type("General"),
            review_type("Frontend", only=("web/**",), lines_changed=2),
        ),
        review_scope="branch",
    )

    assert selection == WorktreeReviewSelection(
        surface=WorktreeReviewSurface(
            changed_paths=(),
            lines_changed=0,
            review_scope="branch",
            base_ref="origin/main",
        ),
        matched_types=(),
    )
