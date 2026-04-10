"""Workflow-facing AI review configuration models and parsers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from ...config.utils import (
    non_negative_int,
    optional_boolean,
    optional_non_blank_string,
    require_mapping,
)
from ...errors import ConfigValidationError
from .review_path_parsing import glob_group_list, glob_list

AI_REVIEW_SCOPE_AUTO = "auto"
AI_REVIEW_SCOPE_WORKTREE = "worktree"
AI_REVIEW_SCOPE_BRANCH = "branch"
_CONFIGURED_AI_REVIEW_SCOPES = {
    AI_REVIEW_SCOPE_AUTO,
    AI_REVIEW_SCOPE_WORKTREE,
    AI_REVIEW_SCOPE_BRANCH,
}
ConfiguredAiReviewScope = Literal["auto", "worktree", "branch"]
ResolvedAiReviewScope = Literal["worktree", "branch"]


@dataclass(frozen=True, slots=True)
class ReviewPathTriggers:
    only: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    require_all: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewCodexConfig:
    model: str | None = None
    reasoning_effort: str | None = None
    fast_mode: bool | None = None


@dataclass(frozen=True, slots=True)
class WorkflowReviewType:
    review_name: str
    prompt_ref: str
    codex: ReviewCodexConfig = ReviewCodexConfig()
    max_runs_per_execution: int | None = None
    lines_changed: int | None = None
    files_changed: int | None = None
    paths: ReviewPathTriggers = ReviewPathTriggers()


@dataclass(frozen=True, slots=True)
class StateAiReviewConfig:
    refs: tuple[str, ...] = ()
    scope: ConfiguredAiReviewScope = AI_REVIEW_SCOPE_AUTO


def parse_review_types(
    config: Mapping[str, Any], review_sections: Mapping[str, str]
) -> dict[str, WorkflowReviewType]:
    raw_ai_review = config.get("ai_review")
    if raw_ai_review is None:
        return {}
    ai_review = require_mapping(raw_ai_review, "ai_review")
    unexpected_keys = set(ai_review.keys()) - {"types"}
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"ai_review has unsupported keys: {names}")
    raw_types = ai_review.get("types")
    if raw_types is None:
        return {}
    if not isinstance(raw_types, Mapping):
        raise ConfigValidationError("ai_review.types must be an object")

    review_types: dict[str, WorkflowReviewType] = {}
    for raw_name, raw_definition in raw_types.items():
        review_name = str(raw_name).strip()
        field_name = f"ai_review.types.{review_name or '<blank>'}"
        if not review_name:
            raise ConfigValidationError("ai_review.types keys must not be blank")
        normalized_name = normalize_review_name(review_name)
        if normalized_name in review_types:
            raise ConfigValidationError(
                f"ai_review.types contains duplicate normalized review {review_name!r}"
            )
        definition = require_mapping(raw_definition, field_name)
        unexpected_keys = set(definition.keys()) - {
            "prompt",
            "codex",
            "max_runs_per_execution",
            "lines_changed",
            "files_changed",
            "paths",
        }
        if unexpected_keys:
            names = ", ".join(sorted(map(str, unexpected_keys)))
            raise ConfigValidationError(f"{field_name} has unsupported keys: {names}")
        prompt_ref = _review_prompt_ref(
            definition.get("prompt"),
            f"{field_name}.prompt",
            review_sections,
        )
        review_types[normalized_name] = WorkflowReviewType(
            review_name=review_name,
            prompt_ref=prompt_ref,
            codex=_review_codex_config(definition.get("codex"), field_name),
            max_runs_per_execution=_optional_non_negative_int(
                definition.get("max_runs_per_execution"),
                f"{field_name}.max_runs_per_execution",
            ),
            lines_changed=_optional_non_negative_int(
                definition.get("lines_changed"),
                f"{field_name}.lines_changed",
            ),
            files_changed=_optional_non_negative_int(
                definition.get("files_changed"),
                f"{field_name}.files_changed",
            ),
            paths=_review_paths(definition.get("paths"), field_name),
        )
    return review_types


def parse_state_ai_review(
    raw_ai_review: Any,
    field_name: str,
    review_types: Mapping[str, WorkflowReviewType],
) -> StateAiReviewConfig:
    review_field = f"{field_name}.ai_review"
    if raw_ai_review is None:
        return StateAiReviewConfig()
    if isinstance(raw_ai_review, Mapping):
        ai_review = require_mapping(raw_ai_review, review_field)
        unexpected_keys = set(ai_review.keys()) - {"types", "scope"}
        if unexpected_keys:
            names = ", ".join(sorted(map(str, unexpected_keys)))
            raise ConfigValidationError(f"{review_field} has unsupported keys: {names}")
        if "types" not in ai_review:
            raise ConfigValidationError(f"{review_field}.types is required")
        return StateAiReviewConfig(
            refs=_normalized_state_review_refs(
                ai_review.get("types"),
                f"{review_field}.types",
                review_types,
            ),
            scope=_configured_review_scope(
                ai_review.get("scope"), f"{review_field}.scope"
            ),
        )
    return StateAiReviewConfig(
        refs=_normalized_state_review_refs(raw_ai_review, review_field, review_types),
        scope=AI_REVIEW_SCOPE_AUTO,
    )


def parse_state_review_refs(
    raw_ai_review: Any,
    field_name: str,
    review_types: Mapping[str, WorkflowReviewType],
) -> tuple[str, ...]:
    return parse_state_ai_review(raw_ai_review, field_name, review_types).refs


def resolve_ai_review_scope(
    configured_scope: ConfiguredAiReviewScope,
    *,
    completion_enabled: bool,
) -> ResolvedAiReviewScope:
    if configured_scope == AI_REVIEW_SCOPE_AUTO:
        return (
            AI_REVIEW_SCOPE_BRANCH if completion_enabled else AI_REVIEW_SCOPE_WORKTREE
        )
    return configured_scope


def _normalized_state_review_refs(
    raw_refs: Any,
    field_name: str,
    review_types: Mapping[str, WorkflowReviewType],
) -> tuple[str, ...]:
    refs: list[str] = []
    if isinstance(raw_refs, str):
        refs = [_review_ref(raw_refs, field_name)]
    elif isinstance(raw_refs, list):
        refs = [_review_ref(value, field_name) for value in raw_refs]
        if not refs:
            raise ConfigValidationError(f"{field_name} must not be empty")
    else:
        raise ConfigValidationError(f"{field_name} must be a string or list of strings")

    seen: set[str] = set()
    normalized_refs: list[str] = []
    for review_ref in refs:
        normalized_ref = normalize_review_name(review_ref)
        if normalized_ref in seen:
            raise ConfigValidationError(
                f"{field_name} must not contain duplicate normalized reviews"
            )
        if normalized_ref not in review_types:
            raise ConfigValidationError(
                f"{field_name} references missing review type {review_ref!r}"
            )
        seen.add(normalized_ref)
        normalized_refs.append(normalized_ref)
    return tuple(normalized_refs)


def _configured_review_scope(value: Any, field_name: str) -> ConfiguredAiReviewScope:
    if value is None:
        return AI_REVIEW_SCOPE_AUTO
    if not isinstance(value, str):
        raise ConfigValidationError(f"{field_name} must be a string")
    scope = value.strip().lower()
    if scope not in _CONFIGURED_AI_REVIEW_SCOPES:
        names = ", ".join(sorted(_CONFIGURED_AI_REVIEW_SCOPES))
        raise ConfigValidationError(f"{field_name} must be one of: {names}")
    return cast(ConfiguredAiReviewScope, scope)


def normalize_review_name(review_name: str) -> str:
    return review_name.strip().lower()


def _review_prompt_ref(
    value: Any, field_name: str, review_sections: Mapping[str, str]
) -> str:
    prompt_ref = _review_ref(value, field_name)
    if not review_sections:
        raise ConfigValidationError(
            "ai_review requires named `# review:` sections in the workflow body"
        )
    if prompt_ref not in review_sections:
        raise ConfigValidationError(
            f"{field_name} references missing review section {prompt_ref!r}"
        )
    return prompt_ref


def _review_paths(raw_paths: Any, field_name: str) -> ReviewPathTriggers:
    paths_field = f"{field_name}.paths"
    paths = require_mapping(raw_paths, paths_field)
    unexpected_keys = set(paths.keys()) - {
        "only",
        "include",
        "exclude",
        "require_all",
    }
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"{paths_field} has unsupported keys: {names}")
    return ReviewPathTriggers(
        only=glob_list(paths.get("only"), f"{paths_field}.only"),
        include=glob_list(paths.get("include"), f"{paths_field}.include"),
        exclude=glob_list(paths.get("exclude"), f"{paths_field}.exclude"),
        require_all=glob_group_list(
            paths.get("require_all"), f"{paths_field}.require_all"
        ),
    )


def _review_codex_config(raw_codex: Any, field_name: str) -> ReviewCodexConfig:
    codex_field = f"{field_name}.codex"
    codex = require_mapping(raw_codex, codex_field)
    unexpected_keys = set(codex.keys()) - {"model", "reasoning_effort", "fast_mode"}
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"{codex_field} has unsupported keys: {names}")
    return ReviewCodexConfig(
        model=optional_non_blank_string(codex.get("model"), f"{codex_field}.model"),
        reasoning_effort=optional_non_blank_string(
            codex.get("reasoning_effort"), f"{codex_field}.reasoning_effort"
        ),
        fast_mode=optional_boolean(codex.get("fast_mode"), f"{codex_field}.fast_mode"),
    )


def _optional_non_negative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return non_negative_int(value, field_name, 0)


def _review_ref(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigValidationError(f"{field_name} must be a string or list of strings")
    review_ref = value.strip()
    if not review_ref:
        raise ConfigValidationError(f"{field_name} entries must not be blank")
    return review_ref
