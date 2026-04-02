"""Generic tracker bootstrap helpers used by `cf init`."""

from __future__ import annotations

from typing import Protocol

from .linear.bootstrap import (
    LinearBootstrapper,
    LinearBootstrapProject,
    LinearBootstrapState,
    LinearBootstrapTeam,
)


class ProjectBootstrapper(Protocol):
    async def close(self) -> None: ...

    async def resolve_project(
        self, reference: str
    ) -> LinearBootstrapProject | None: ...

    async def resolve_team(self, reference: str) -> LinearBootstrapTeam: ...

    async def create_project(
        self, *, name: str, team: LinearBootstrapTeam
    ) -> LinearBootstrapProject: ...

    async def ensure_states(
        self,
        *,
        team: LinearBootstrapTeam,
        required_states: tuple[tuple[str, str], ...],
    ) -> tuple[LinearBootstrapState, ...]: ...


def build_tracker_bootstrapper(
    *, tracker_kind: str, api_key: str
) -> ProjectBootstrapper:
    if tracker_kind != "linear":
        raise ValueError(f"unsupported tracker bootstrap kind: {tracker_kind}")
    return LinearBootstrapper(api_key=api_key)
