from __future__ import annotations

from itertools import permutations
from pathlib import Path

import pytest
from pytestarch import Rule, get_evaluable_architecture

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "code_factory"
OPERATOR_PACKAGES = ("application", "observability")
NON_OPERATOR_PACKAGES = (
    "coding_agents",
    "config",
    "prompts",
    "runtime",
    "trackers",
    "workflow",
    "workspace",
)
SCOPED_IMPLEMENTATION_PACKAGES = (
    ("application.dashboard", "application"),
    ("coding_agents.codex.tools.tracker", "coding_agents"),
    ("runtime.worker.quality_gates", "runtime"),
    ("trackers.linear.ops", "trackers"),
)
SIBLING_IMPLEMENTATION_GROUPS = (
    ("trackers.linear", "trackers.memory"),
    ("workspace.review", "workspace.ai_review"),
)
SIBLING_IMPLEMENTATION_PAIRS = tuple(
    (left, right)
    for group in SIBLING_IMPLEMENTATION_GROUPS
    for left, right in permutations(group, 2)
)


@pytest.fixture(scope="session")
def evaluable_architecture():
    return get_evaluable_architecture(str(ROOT), str(SRC_ROOT))


@pytest.fixture(scope="session")
def module_prefix(evaluable_architecture) -> str:
    return next(
        module
        for module in evaluable_architecture.modules
        if module.endswith(".src.code_factory")
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


def test_non_operator_packages_do_not_depend_on_operator_layers(
    evaluable_architecture, module_prefix: str
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(
            [f"{module_prefix}.{package}" for package in NON_OPERATOR_PACKAGES]
        )
        .should_not()
        .import_modules_that()
        .are_sub_modules_of(
            [f"{module_prefix}.{package}" for package in OPERATOR_PACKAGES]
        )
    )
    rule.assert_applies(evaluable_architecture)


@pytest.mark.parametrize(
    ("implementation_package", "owner_package"),
    SCOPED_IMPLEMENTATION_PACKAGES,
)
def test_nested_implementation_packages_are_only_imported_inside_owner_package(
    evaluable_architecture,
    module_prefix: str,
    implementation_package: str,
    owner_package: str,
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(f"{module_prefix}.{implementation_package}")
        .should_not()
        .be_imported_by_modules_except_modules_that()
        .are_sub_modules_of(f"{module_prefix}.{owner_package}")
    )
    rule.assert_applies(evaluable_architecture)


@pytest.mark.parametrize(
    ("left_package", "right_package"),
    SIBLING_IMPLEMENTATION_PAIRS,
)
def test_sibling_implementation_packages_do_not_depend_on_each_other(
    evaluable_architecture,
    module_prefix: str,
    left_package: str,
    right_package: str,
) -> None:
    rule = (
        Rule()
        .modules_that()
        .are_sub_modules_of(f"{module_prefix}.{left_package}")
        .should_not()
        .import_modules_that()
        .are_sub_modules_of(f"{module_prefix}.{right_package}")
    )
    rule.assert_applies(evaluable_architecture)


def test_all_source_files_stay_under_three_hundred_fifty_lines() -> None:
    oversized_files = []
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > 350:
            oversized_files.append((path.relative_to(ROOT), line_count))
    assert oversized_files == []
