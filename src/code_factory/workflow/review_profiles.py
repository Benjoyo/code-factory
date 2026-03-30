"""Workflow-facing AI review configuration models and parsers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..config.utils import (
    non_negative_int,
    optional_non_blank_string,
    require_mapping,
)
from ..errors import ConfigValidationError


@dataclass(frozen=True, slots=True)
class ReviewPathTriggers:
    """Path-based filters used to decide whether one review type should run."""

    only: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowReviewType:
    """Reusable AI review definition loaded from workflow front matter."""

    review_name: str
    prompt_ref: str
    model: str | None = None
    reasoning_effort: str | None = None
    lines_changed: int | None = None
    paths: ReviewPathTriggers = ReviewPathTriggers()


def parse_review_types(
    config: Mapping[str, Any], review_sections: Mapping[str, str]
) -> dict[str, WorkflowReviewType]:
    """Validate and normalize reusable workflow AI review definitions."""

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
            "model",
            "reasoning_effort",
            "lines_changed",
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
            model=optional_non_blank_string(
                definition.get("model"), f"{field_name}.model"
            ),
            reasoning_effort=optional_non_blank_string(
                definition.get("reasoning_effort"),
                f"{field_name}.reasoning_effort",
            ),
            lines_changed=_optional_non_negative_int(
                definition.get("lines_changed"),
                f"{field_name}.lines_changed",
            ),
            paths=_review_paths(definition.get("paths"), field_name),
        )
    return review_types


def parse_state_review_refs(
    raw_ai_review: Any,
    field_name: str,
    review_types: Mapping[str, WorkflowReviewType],
) -> tuple[str, ...]:
    """Validate one state's references to reusable AI review definitions."""

    review_field = f"{field_name}.ai_review"
    refs: list[str] = []
    if raw_ai_review is None:
        return ()
    if isinstance(raw_ai_review, str):
        refs = [_review_ref(raw_ai_review, review_field)]
    elif isinstance(raw_ai_review, list):
        refs = [_review_ref(value, review_field) for value in raw_ai_review]
        if not refs:
            raise ConfigValidationError(f"{review_field} must not be empty")
    else:
        raise ConfigValidationError(
            f"{review_field} must be a string or list of strings"
        )

    seen: set[str] = set()
    normalized_refs: list[str] = []
    for review_ref in refs:
        normalized_ref = normalize_review_name(review_ref)
        if normalized_ref in seen:
            raise ConfigValidationError(
                f"{review_field} must not contain duplicate normalized reviews"
            )
        if normalized_ref not in review_types:
            raise ConfigValidationError(
                f"{review_field} references missing review type {review_ref!r}"
            )
        seen.add(normalized_ref)
        normalized_refs.append(normalized_ref)
    return tuple(normalized_refs)


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
    unexpected_keys = set(paths.keys()) - {"only", "include", "exclude"}
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"{paths_field} has unsupported keys: {names}")
    return ReviewPathTriggers(
        only=_glob_list(paths.get("only"), f"{paths_field}.only"),
        include=_glob_list(paths.get("include"), f"{paths_field}.include"),
        exclude=_glob_list(paths.get("exclude"), f"{paths_field}.exclude"),
    )


def _glob_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigValidationError(f"{field_name} must be a list of strings")
    globs: list[str] = []
    seen: set[str] = set()
    for raw_glob in value:
        if not isinstance(raw_glob, str):
            raise ConfigValidationError(f"{field_name} must be a list of strings")
        glob = raw_glob.strip()
        if not glob:
            raise ConfigValidationError(f"{field_name} entries must not be blank")
        if glob in seen:
            raise ConfigValidationError(f"{field_name} must not contain duplicates")
        seen.add(glob)
        globs.append(glob)
    if not globs:
        raise ConfigValidationError(f"{field_name} must not be empty")
    return tuple(globs)


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
