"""Shared validation helpers for state profile list and name fields."""

from __future__ import annotations

from typing import Any

from ..errors import ConfigValidationError
from ..issues import normalize_issue_state


def state_name_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigValidationError(f"{field_name} must be a list of strings")
    states: list[str] = []
    seen: set[str] = set()
    for raw_state in value:
        state_name = required_state_name(raw_state, field_name)
        normalized = normalize_issue_state(state_name)
        if normalized in seen:
            raise ConfigValidationError(
                f"{field_name} must not contain duplicate normalized states"
            )
        seen.add(normalized)
        states.append(state_name)
    return tuple(states)


def optional_state_name(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return required_state_name(value, field_name)


def required_state_name(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigValidationError(f"{field_name} must be a string")
    state_name = value.strip()
    if not state_name:
        raise ConfigValidationError(f"{field_name} must not be blank")
    return state_name


def skill_name_list(value: Any, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ConfigValidationError(f"{field_name} must be a list of strings")
    skills: list[str] = []
    seen: set[str] = set()
    for raw_skill in value:
        if not isinstance(raw_skill, str):
            raise ConfigValidationError(f"{field_name} must be a list of strings")
        skill_name = raw_skill.strip()
        if not skill_name:
            raise ConfigValidationError(f"{field_name} entries must not be blank")
        if skill_name in seen:
            raise ConfigValidationError(f"{field_name} must not contain duplicates")
        seen.add(skill_name)
        skills.append(skill_name)
    return tuple(skills)
