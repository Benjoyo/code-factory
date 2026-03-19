"""Parsing helpers and models for optional state-specific workflow profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..config.utils import optional_non_blank_string, require_mapping
from ..errors import ConfigValidationError
from ..issues import normalize_issue_state


@dataclass(frozen=True, slots=True)
class StateCodexOverride:
    """Optional coding-agent overrides allowed for a specific tracker state."""

    model: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStateProfile:
    """Prompt and codex settings selected for a specific tracker state."""

    state_name: str
    prompt_refs: tuple[str, ...]
    codex: StateCodexOverride

    def codex_model(self, default: str | None) -> str | None:
        return self.codex.model if self.codex.model is not None else default

    def codex_reasoning_effort(self, default: str | None) -> str | None:
        return (
            self.codex.reasoning_effort
            if self.codex.reasoning_effort is not None
            else default
        )


def parse_state_profiles(
    config: Mapping[str, Any], prompt_sections: Mapping[str, str]
) -> dict[str, WorkflowStateProfile]:
    """Validate and normalize optional state profiles from workflow front matter."""

    raw_states = config.get("states")
    if raw_states is None:
        return {}
    if not isinstance(raw_states, Mapping):
        raise ConfigValidationError("states must be an object")
    if not prompt_sections:
        raise ConfigValidationError(
            "states requires named `# prompt:` sections in the workflow body"
        )

    profiles: dict[str, WorkflowStateProfile] = {}
    for raw_state_name, raw_profile in raw_states.items():
        state_name = str(raw_state_name).strip()
        field_name = f"states.{state_name or '<blank>'}"
        if not state_name:
            raise ConfigValidationError("states keys must not be blank")
        normalized_state = normalize_issue_state(state_name)
        if normalized_state in profiles:
            raise ConfigValidationError(
                f"states contains duplicate normalized state {state_name!r}"
            )
        profile = require_mapping(raw_profile, field_name)
        unexpected_keys = set(profile.keys()) - {"prompt", "codex"}
        if unexpected_keys:
            raise ConfigValidationError(
                f"{field_name} has unsupported keys: {', '.join(sorted(map(str, unexpected_keys)))}"
            )
        prompt_refs = _prompt_refs(profile.get("prompt"), field_name, prompt_sections)
        codex = _codex_override(profile.get("codex"), field_name)
        profiles[normalized_state] = WorkflowStateProfile(
            state_name=state_name,
            prompt_refs=prompt_refs,
            codex=codex,
        )
    return profiles


def _prompt_refs(
    raw_prompt: Any,
    field_name: str,
    prompt_sections: Mapping[str, str],
) -> tuple[str, ...]:
    prompt_field = f"{field_name}.prompt"
    refs: list[str] = []
    if isinstance(raw_prompt, str):
        refs = [_prompt_ref(raw_prompt, prompt_field)]
    elif isinstance(raw_prompt, list):
        refs = [_prompt_ref(value, prompt_field) for value in raw_prompt]
        if not refs:
            raise ConfigValidationError(f"{prompt_field} must not be empty")
    else:
        raise ConfigValidationError(
            f"{prompt_field} must be a string or list of strings"
        )

    for prompt_ref in refs:
        if prompt_ref not in prompt_sections:
            raise ConfigValidationError(
                f"{prompt_field} references missing prompt section {prompt_ref!r}"
            )
    return tuple(refs)


def _prompt_ref(raw_prompt_ref: Any, field_name: str) -> str:
    if not isinstance(raw_prompt_ref, str):
        raise ConfigValidationError(f"{field_name} must be a string or list of strings")
    prompt_ref = raw_prompt_ref.strip()
    if not prompt_ref:
        raise ConfigValidationError(f"{field_name} entries must not be blank")
    return prompt_ref


def _codex_override(raw_codex: Any, field_name: str) -> StateCodexOverride:
    codex_field = f"{field_name}.codex"
    codex = require_mapping(raw_codex, codex_field)
    unexpected_keys = set(codex.keys()) - {"model", "reasoning_effort"}
    if unexpected_keys:
        raise ConfigValidationError(
            f"{codex_field} has unsupported keys: {', '.join(sorted(map(str, unexpected_keys)))}"
        )
    return StateCodexOverride(
        model=optional_non_blank_string(codex.get("model"), f"{codex_field}.model"),
        reasoning_effort=optional_non_blank_string(
            codex.get("reasoning_effort"), f"{codex_field}.reasoning_effort"
        ),
    )
