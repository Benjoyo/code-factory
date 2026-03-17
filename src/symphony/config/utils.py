from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ..errors import ConfigValidationError
from ..issues import normalize_issue_state


def require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise ConfigValidationError(f"{field_name} must be an object")


def coerce_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigValidationError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            raise ConfigValidationError(f"{field_name} must be an integer")
        try:
            return int(stripped)
        except ValueError as exc:
            raise ConfigValidationError(f"{field_name} must be an integer") from exc
    raise ConfigValidationError(f"{field_name} must be an integer")


def positive_int(value: Any, field_name: str, default: int) -> int:
    if value is None:
        return default
    parsed = coerce_int(value, field_name)
    if parsed <= 0:
        raise ConfigValidationError(f"{field_name} must be greater than 0")
    return parsed


def non_negative_int(value: Any, field_name: str, default: int) -> int:
    if value is None:
        return default
    parsed = coerce_int(value, field_name)
    if parsed < 0:
        raise ConfigValidationError(f"{field_name} must be greater than or equal to 0")
    return parsed


def optional_non_negative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return non_negative_int(value, field_name, 0)


def optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ConfigValidationError(f"{field_name} must be a string")


def string_with_default(value: Any, field_name: str, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    raise ConfigValidationError(f"{field_name} must be a string")


def required_command(value: Any, field_name: str, default: str) -> str:
    command = string_with_default(value, field_name, default)
    if command == "":
        raise ConfigValidationError(f"{field_name} can't be blank")
    return command


def string_list(
    value: Any, field_name: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list):
        raise ConfigValidationError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigValidationError(f"{field_name} must be a list of strings")
        items.append(item)
    return tuple(items)


def normalize_state_limits(value: Any, field_name: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigValidationError(f"{field_name} must be an object")
    limits: dict[str, int] = {}
    for raw_state, raw_limit in value.items():
        state_name = str(raw_state)
        if not state_name:
            raise ConfigValidationError(f"{field_name} state names must not be blank")
        limit = coerce_int(raw_limit, field_name)
        if limit <= 0:
            raise ConfigValidationError(
                f"{field_name} limits must be positive integers"
            )
        limits[normalize_issue_state(state_name)] = limit
    return limits


def boolean(value: Any, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigValidationError(f"{field_name} must be a boolean")


def normalize_path_token(value: str) -> str | None:
    env_name = env_reference_name(value)
    if env_name is None:
        return value
    return os.getenv(env_name)


def resolve_path_value(value: Any, default: str, field_name: str) -> str:
    token: str
    if value is None:
        token = default
    elif isinstance(value, str):
        resolved_token = normalize_path_token(value)
        token = default if resolved_token in {None, ""} else str(resolved_token)
    else:
        raise ConfigValidationError(f"{field_name} must be a string")
    return os.path.abspath(os.path.expanduser(token))


def normalize_secret_value(value: str | None) -> str | None:
    if value == "":
        return None
    return value


def env_reference_name(value: str) -> str | None:
    if value.startswith("$") and len(value) > 1:
        env_name = value[1:]
        if (env_name[0].isalpha() or env_name[0] == "_") and env_name.replace(
            "_", "A"
        ).isalnum():
            return env_name
    return None


def resolve_env_value(value: str, fallback: str | None) -> str | None:
    env_name = env_reference_name(value)
    if env_name is None:
        return value
    env_value = os.getenv(env_name)
    if env_value is None:
        return fallback
    if env_value == "":
        return None
    return env_value


def resolve_secret_setting(
    value: Any, fallback: str | None, field_name: str
) -> str | None:
    if value is None:
        return normalize_secret_value(fallback)
    if not isinstance(value, str):
        raise ConfigValidationError(f"{field_name} must be a string")
    return normalize_secret_value(resolve_env_value(value, fallback))


def normalize_keys(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): normalize_keys(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [normalize_keys(item) for item in value]
    return value
