"""Helpers for parsing workflow AI review path-trigger config."""

from __future__ import annotations

from typing import Any

from ...errors import ConfigValidationError


def glob_list(value: Any, field_name: str) -> tuple[str, ...]:
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


def glob_group_list(value: Any, field_name: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigValidationError(
            f"{field_name} must be a list of non-empty glob lists"
        )
    groups: list[tuple[str, ...]] = []
    for index, raw_group in enumerate(value):
        if not isinstance(raw_group, list):
            raise ConfigValidationError(
                f"{field_name}[{index}] must be a list of strings"
            )
        groups.append(glob_list(raw_group, f"{field_name}[{index}]"))
    if not groups:
        raise ConfigValidationError(f"{field_name} must not be empty")
    return tuple(groups)
