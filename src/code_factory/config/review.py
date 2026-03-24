"""Configuration helpers for operator-side PR review worktrees."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ..errors import ConfigValidationError
from .models import ReviewServerSettings, ReviewSettings
from .utils import non_negative_int, optional_string, require_mapping


def parse_review_settings(config: Mapping[str, Any]) -> ReviewSettings:
    review_raw = require_mapping(config.get("review"), "review")
    servers_raw = review_raw.get("servers")
    if servers_raw is None:
        return ReviewSettings(
            temp_root=_optional_review_root(review_raw.get("temp_root")),
            prepare=optional_string(review_raw.get("prepare"), "review.prepare"),
        )
    if not isinstance(servers_raw, list):
        raise ConfigValidationError("review.servers must be a list")
    servers = tuple(
        _parse_review_server(item, index) for index, item in enumerate(servers_raw)
    )
    names = [server.name for server in servers]
    if len(set(names)) != len(names):
        raise ConfigValidationError("review.servers names must be unique")
    if not servers:
        raise ConfigValidationError("review.servers must not be empty")
    return ReviewSettings(
        temp_root=_optional_review_root(review_raw.get("temp_root")),
        prepare=optional_string(review_raw.get("prepare"), "review.prepare"),
        servers=servers,
    )


def _parse_review_server(value: Any, index: int) -> ReviewServerSettings:
    field_name = f"review.servers[{index}]"
    server_raw = require_mapping(value, field_name)
    name = optional_string(server_raw.get("name"), f"{field_name}.name")
    command = optional_string(server_raw.get("command"), f"{field_name}.command")
    if name is None or not name.strip():
        raise ConfigValidationError(f"{field_name}.name can't be blank")
    if command is None or not command.strip():
        raise ConfigValidationError(f"{field_name}.command can't be blank")
    return ReviewServerSettings(
        name=name.strip(),
        command=command,
        base_port=_optional_port(
            server_raw.get("base_port"), f"{field_name}.base_port"
        ),
        url=optional_string(server_raw.get("url"), f"{field_name}.url"),
        open_browser=_optional_boolean(
            server_raw.get("open_browser"), f"{field_name}.open_browser"
        ),
    )


def _optional_port(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    port = non_negative_int(value, field_name, 0)
    if port == 0:
        raise ConfigValidationError(f"{field_name} must be greater than 0")
    return port


def _optional_review_root(value: Any) -> str | None:
    raw = optional_string(value, "review.temp_root")
    if raw is None:
        return None
    token = raw
    if raw.startswith("$") and len(raw) > 1:
        token = os.getenv(raw[1:], "") or ""
    if token.strip() == "":
        return None
    return os.path.abspath(os.path.expanduser(token))


def _optional_boolean(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ConfigValidationError(f"{field_name} must be a boolean")
