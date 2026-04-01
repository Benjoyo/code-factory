"""Shared state-level control models and parsers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...config.utils import (
    boolean,
    non_negative_int,
    optional_non_blank_string,
    require_mapping,
)
from ...errors import ConfigValidationError


@dataclass(frozen=True, slots=True)
class StateHooksOverride:
    """Optional shell hooks enforced for one agent-run state."""

    before_complete: str | None = None
    before_complete_max_feedback_loops: int = 10


@dataclass(frozen=True, slots=True)
class StateCompletionOverride:
    """Optional native completion readiness requirements for one state."""

    require_pushed_head: bool = False
    require_pr: bool = False

    @property
    def enabled(self) -> bool:
        return self.require_pushed_head or self.require_pr


def parse_state_hooks(
    raw_hooks: Any, field_name: str, *, allow_feedback_loops_without_hook: bool
) -> StateHooksOverride:
    hooks_field = f"{field_name}.hooks"
    hooks = require_mapping(raw_hooks, hooks_field)
    unexpected_keys = set(hooks.keys()) - {
        "before_complete",
        "before_complete_max_feedback_loops",
    }
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"{hooks_field} has unsupported keys: {names}")
    before_complete = optional_non_blank_string(
        hooks.get("before_complete"), f"{hooks_field}.before_complete"
    )
    if (
        before_complete is None
        and "before_complete_max_feedback_loops" in hooks
        and not allow_feedback_loops_without_hook
    ):
        raise ConfigValidationError(
            f"{hooks_field}.before_complete_max_feedback_loops requires {hooks_field}.before_complete"
        )
    return StateHooksOverride(
        before_complete=before_complete,
        before_complete_max_feedback_loops=non_negative_int(
            hooks.get("before_complete_max_feedback_loops"),
            f"{hooks_field}.before_complete_max_feedback_loops",
            10,
        ),
    )


def parse_state_completion(
    raw_completion: Any, field_name: str
) -> StateCompletionOverride:
    completion_field = f"{field_name}.completion"
    completion = require_mapping(raw_completion, completion_field)
    unexpected_keys = set(completion.keys()) - {"require_pushed_head", "require_pr"}
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"{completion_field} has unsupported keys: {names}")
    require_pr = boolean(
        completion.get("require_pr"),
        f"{completion_field}.require_pr",
        False,
    )
    return StateCompletionOverride(
        require_pushed_head=require_pr
        or boolean(
            completion.get("require_pushed_head"),
            f"{completion_field}.require_pushed_head",
            False,
        ),
        require_pr=require_pr,
    )
