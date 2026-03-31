"""Parsing helpers and models for optional state-specific workflow profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..config.utils import optional_non_blank_string, require_mapping
from ..errors import ConfigValidationError
from ..issues import normalize_issue_state
from .review_profiles import (
    ConfiguredAiReviewScope,
    ResolvedAiReviewScope,
    WorkflowReviewType,
    parse_state_ai_review,
    resolve_ai_review_scope,
)
from .state_controls import (
    StateCompletionOverride,
    StateHooksOverride,
    parse_state_completion,
    parse_state_hooks,
)
from .state_values import (
    optional_state_name,
    skill_name_list,
    state_name_list,
)


@dataclass(frozen=True, slots=True)
class StateCodexOverride:
    """Optional coding-agent overrides allowed for a specific tracker state."""

    model: str | None = None
    reasoning_effort: str | None = None
    skills: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStateProfile:
    """Prompt and transition settings selected for a specific tracker state."""

    state_name: str
    prompt_refs: tuple[str, ...] = ()
    ai_review_refs: tuple[str, ...] = ()
    ai_review_scope: ConfiguredAiReviewScope = "auto"
    codex: StateCodexOverride = StateCodexOverride()
    completion: StateCompletionOverride = StateCompletionOverride()
    hooks: StateHooksOverride = StateHooksOverride()
    allowed_next_states: tuple[str, ...] = ()
    failure_state: str | None = None
    auto_next_state: str | None = None

    @property
    def is_auto(self) -> bool:
        return self.auto_next_state is not None

    @property
    def is_agent_run(self) -> bool:
        return not self.is_auto

    def resolved_ai_review_scope(self) -> ResolvedAiReviewScope:
        return resolve_ai_review_scope(
            self.ai_review_scope, completion_enabled=self.completion.enabled
        )

    def codex_model(self, default: str | None) -> str | None:
        return self.codex.model if self.codex.model is not None else default

    def codex_reasoning_effort(self, default: str | None) -> str | None:
        return (
            self.codex.reasoning_effort
            if self.codex.reasoning_effort is not None
            else default
        )

    def codex_repo_skill_allowlist(
        self, default: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        return self.codex.skills if self.codex.skills is not None else default

    def allows_next_state(self, state_name: str) -> bool:
        if not self.allowed_next_states:
            return True
        normalized_candidate = normalize_issue_state(state_name)
        return normalized_candidate in {
            normalize_issue_state(allowed) for allowed in self.allowed_next_states
        }


def parse_state_profiles(
    config: Mapping[str, Any],
    prompt_sections: Mapping[str, str],
    review_types: Mapping[str, WorkflowReviewType] | None = None,
) -> dict[str, WorkflowStateProfile]:
    """Validate and normalize optional state profiles from workflow front matter."""

    resolved_review_types = review_types or {}
    raw_states = config.get("states")
    if raw_states is None:
        return {}
    if not isinstance(raw_states, Mapping):
        raise ConfigValidationError("states must be an object")

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
        unexpected_keys = set(profile.keys()) - {
            "prompt",
            "ai_review",
            "codex",
            "completion",
            "hooks",
            "allowed_next_states",
            "failure_state",
            "auto_next_state",
        }
        if unexpected_keys:
            names = ", ".join(sorted(map(str, unexpected_keys)))
            raise ConfigValidationError(f"{field_name} has unsupported keys: {names}")
        prompt_refs = _prompt_refs(
            profile.get("prompt"),
            field_name,
            prompt_sections,
        )
        ai_review = parse_state_ai_review(
            profile.get("ai_review"),
            field_name,
            resolved_review_types,
        )
        allowed_next_states = state_name_list(
            profile.get("allowed_next_states"), f"{field_name}.allowed_next_states"
        )
        failure_state = optional_state_name(
            profile.get("failure_state"), f"{field_name}.failure_state"
        )
        auto_next_state = optional_state_name(
            profile.get("auto_next_state"), f"{field_name}.auto_next_state"
        )
        codex = _codex_override(profile.get("codex"), field_name)
        completion = parse_state_completion(profile.get("completion"), field_name)
        hooks = parse_state_hooks(
            profile.get("hooks"),
            field_name,
            allow_feedback_loops_without_hook=completion.enabled,
        )
        if prompt_refs and auto_next_state is not None:
            raise ConfigValidationError(
                f"{field_name} cannot define both prompt and auto_next_state"
            )
        if auto_next_state is None and not prompt_refs:
            raise ConfigValidationError(
                f"{field_name} must define either prompt or auto_next_state"
            )
        if auto_next_state is not None and (
            codex.model is not None
            or codex.reasoning_effort is not None
            or codex.skills is not None
        ):
            raise ConfigValidationError(
                f"{field_name}.codex is not supported for auto states"
            )
        if auto_next_state is not None and hooks.before_complete is not None:
            raise ConfigValidationError(
                f"{field_name}.hooks is not supported for auto states"
            )
        if auto_next_state is not None and ai_review.refs:
            raise ConfigValidationError(
                f"{field_name}.ai_review is not supported for auto states"
            )
        if auto_next_state is not None and completion.enabled:
            raise ConfigValidationError(
                f"{field_name}.completion is not supported for auto states"
            )
        if (
            failure_state is not None
            and normalize_issue_state(failure_state) == normalized_state
        ):
            raise ConfigValidationError(
                f"{field_name}.failure_state must not equal the current state"
            )
        profiles[normalized_state] = WorkflowStateProfile(
            state_name=state_name,
            prompt_refs=prompt_refs,
            ai_review_refs=ai_review.refs,
            ai_review_scope=ai_review.scope,
            codex=codex,
            completion=completion,
            hooks=hooks,
            allowed_next_states=allowed_next_states,
            failure_state=failure_state,
            auto_next_state=auto_next_state,
        )
    return profiles


def _prompt_refs(
    raw_prompt: Any,
    field_name: str,
    prompt_sections: Mapping[str, str],
) -> tuple[str, ...]:
    prompt_field = f"{field_name}.prompt"
    refs: list[str] = []
    if raw_prompt is None:
        return ()
    if not prompt_sections:
        raise ConfigValidationError(
            "states requires named `# prompt:` sections in the workflow body"
        )
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
    unexpected_keys = set(codex.keys()) - {"model", "reasoning_effort", "skills"}
    if unexpected_keys:
        names = ", ".join(sorted(map(str, unexpected_keys)))
        raise ConfigValidationError(f"{codex_field} has unsupported keys: {names}")
    return StateCodexOverride(
        model=optional_non_blank_string(codex.get("model"), f"{codex_field}.model"),
        reasoning_effort=optional_non_blank_string(
            codex.get("reasoning_effort"), f"{codex_field}.reasoning_effort"
        ),
        skills=skill_name_list(codex.get("skills"), f"{codex_field}.skills"),
    )
