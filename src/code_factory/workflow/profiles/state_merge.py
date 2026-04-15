"""Workflow-facing merge configuration models and parsers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from ...config.utils import require_mapping
from ...errors import ConfigValidationError

MERGE_MODE_AGENT_ONLY = "agent_only"
MERGE_MODE_NATIVE_THEN_AGENT = "native_then_agent"
_STATE_MERGE_MODES = {
    MERGE_MODE_AGENT_ONLY,
    MERGE_MODE_NATIVE_THEN_AGENT,
}
StateMergeMode = Literal["agent_only", "native_then_agent"]


@dataclass(frozen=True, slots=True)
class StateMergeOverride:
    mode: StateMergeMode = MERGE_MODE_AGENT_ONLY


def parse_state_merge(raw_merge: Any, field_name: str) -> StateMergeOverride:
    merge_field = f"{field_name}.merge"
    merge = require_mapping(raw_merge, merge_field)
    unexpected_keys = set(merge.keys()) - {"mode"}
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"{merge_field} has unsupported keys: {names}")
    raw_mode = merge.get("mode")
    if raw_mode is None:
        return StateMergeOverride()
    if not isinstance(raw_mode, str):
        raise ConfigValidationError(f"{merge_field}.mode must be a string")
    mode = raw_mode.strip()
    if not mode:
        raise ConfigValidationError(f"{merge_field}.mode must not be blank")
    if mode not in _STATE_MERGE_MODES:
        expected = ", ".join(sorted(_STATE_MERGE_MODES))
        raise ConfigValidationError(f"{merge_field}.mode must be one of: {expected}")
    return StateMergeOverride(mode=cast(StateMergeMode, mode))
