"""Workflow model types shared between the loader, store, and orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from ..config.models import Settings
from ..errors import ConfigValidationError
from ..issues import normalize_issue_state
from .state_profiles import WorkflowStateProfile


def utc_now() -> datetime:
    """Default factory so snapshots record load time in UTC."""

    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FileStamp:
    """Filesystem fingerprint used to detect workflow file changes."""

    mtime: int
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Parsed workflow document split into configuration and prompt template."""

    config: dict[str, Any]
    prompt_template: str
    prompt_sections: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    """Versioned, validated workflow payload consumed by the running service."""

    version: int
    path: str
    stamp: FileStamp
    definition: WorkflowDefinition
    settings: Settings
    state_profiles: dict[str, WorkflowStateProfile] = field(default_factory=dict)
    loaded_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for normalized_state, profile in self.state_profiles.items():
            if (
                normalize_issue_state(self.failure_state_for_state(profile.state_name))
                == normalized_state
            ):
                raise ConfigValidationError(
                    f"states.{profile.state_name}.resolved failure_state must not equal the current state"
                )

    @property
    def prompt_template(self) -> str:
        """Expose the prompt directly so callers do not reach into `definition`."""

        return self.definition.prompt_template

    def prompt_template_for_state(self, state_name: str | None) -> str:
        """Resolve the state-specific prompt body when the workflow defines profiles."""

        profile = self.state_profile(state_name)
        if profile is None or not profile.prompt_refs:
            return self.definition.prompt_template
        return "\n\n".join(
            self.definition.prompt_sections[prompt_ref]
            for prompt_ref in profile.prompt_refs
        ).strip()

    def settings_for_state(self, state_name: str | None) -> Settings:
        """Return the effective settings for the provided tracker state."""

        profile = self.state_profile(state_name)
        if profile is None:
            return self.settings
        return replace(
            self.settings,
            coding_agent=replace(
                self.settings.coding_agent,
                model=profile.codex_model(self.settings.coding_agent.model),
                reasoning_effort=profile.codex_reasoning_effort(
                    self.settings.coding_agent.reasoning_effort
                ),
                repo_skill_allowlist=profile.codex_repo_skill_allowlist(
                    self.settings.coding_agent.repo_skill_allowlist
                ),
            ),
        )

    def failure_state_for_state(self, state_name: str | None) -> str:
        profile = self.state_profile(state_name)
        if profile is None or profile.failure_state is None:
            return self.settings.failure_state
        return profile.failure_state

    def state_profile(self, state_name: str | None) -> WorkflowStateProfile | None:
        return self.state_profiles.get(normalize_issue_state(state_name))


@dataclass(slots=True)
class WorkflowStoreState:
    """Mutable actor state that preserves the last known good workflow version."""

    path: str
    stamp: FileStamp
    workflow: WorkflowDefinition
    version: int
    last_reload_error: Any = None
