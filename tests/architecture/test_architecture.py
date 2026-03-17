from __future__ import annotations

from pathlib import Path

import pytest
from pytestarch import Rule, get_evaluable_architecture

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "symphony"
ALLOWED_TOP_LEVEL_ENTRIES = {
    "__init__.py",
    "__main__.py",
    "application",
    "cli.py",
    "coding_agents",
    "config",
    "errors.py",
    "issues.py",
    "observability",
    "prompts",
    "runtime",
    "trackers",
    "workflow",
    "workspace",
}


@pytest.fixture(scope="session")
def evaluable_architecture():
    return get_evaluable_architecture(str(ROOT), str(SRC_ROOT))


@pytest.fixture(scope="session")
def module_prefix(evaluable_architecture) -> str:
    return next(
        module
        for module in evaluable_architecture.modules
        if module.endswith(".src.symphony")
    )


def test_concrete_coding_agent_is_only_imported_inside_coding_agents_package(
    evaluable_architecture, module_prefix: str
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(f"{module_prefix}.coding_agents.codex")
        .should_not()
        .be_imported_by_modules_except_modules_that()
        .are_sub_modules_of(f"{module_prefix}.coding_agents")
    )
    rule.assert_applies(evaluable_architecture)


def test_concrete_tracker_packages_are_only_imported_inside_trackers_package(
    evaluable_architecture, module_prefix: str
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(
            [
                f"{module_prefix}.trackers.linear",
                f"{module_prefix}.trackers.memory",
            ]
        )
        .should_not()
        .be_imported_by_modules_except_modules_that()
        .are_sub_modules_of(f"{module_prefix}.trackers")
    )
    rule.assert_applies(evaluable_architecture)


def test_runtime_does_not_depend_on_concrete_integrations(
    evaluable_architecture, module_prefix: str
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(f"{module_prefix}.runtime")
        .should_not()
        .import_modules_that()
        .are_sub_modules_of(
            [
                f"{module_prefix}.coding_agents.codex",
                f"{module_prefix}.trackers.linear",
                f"{module_prefix}.trackers.memory",
            ]
        )
    )
    rule.assert_applies(evaluable_architecture)


def test_application_and_observability_stay_above_runtime_and_integrations(
    evaluable_architecture, module_prefix: str
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(
            [
                f"{module_prefix}.application",
                f"{module_prefix}.observability",
            ]
        )
        .should_not()
        .import_modules_that()
        .are_sub_modules_of(
            [
                f"{module_prefix}.trackers",
                f"{module_prefix}.coding_agents",
            ]
        )
    )
    rule.assert_applies(evaluable_architecture)


def test_codex_does_not_depend_on_tracker_implementations(
    evaluable_architecture, module_prefix: str
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(f"{module_prefix}.coding_agents.codex")
        .should_not()
        .import_modules_that()
        .are_sub_modules_of(
            [
                f"{module_prefix}.trackers.linear",
                f"{module_prefix}.trackers.memory",
            ]
        )
    )
    rule.assert_applies(evaluable_architecture)


def test_tracker_implementations_do_not_depend_on_coding_agent_packages(
    evaluable_architecture, module_prefix: str
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(
            [
                f"{module_prefix}.trackers.linear",
                f"{module_prefix}.trackers.memory",
            ]
        )
        .should_not()
        .import_modules_that()
        .are_sub_modules_of(f"{module_prefix}.coding_agents")
    )
    rule.assert_applies(evaluable_architecture)


def test_top_level_package_layout_is_curated() -> None:
    actual_entries = {
        path.name for path in SRC_ROOT.iterdir() if path.name != "__pycache__"
    }
    assert actual_entries == ALLOWED_TOP_LEVEL_ENTRIES


def test_no_legacy_top_level_concrete_packages_exist() -> None:
    assert not (SRC_ROOT / "codex").exists()
    assert not (SRC_ROOT / "linear").exists()


def test_all_source_files_stay_under_three_hundred_lines() -> None:
    oversized_files = []
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > 300:
            oversized_files.append((path.relative_to(ROOT), line_count))
    assert oversized_files == []
