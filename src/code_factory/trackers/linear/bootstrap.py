from __future__ import annotations

"""Linear project and workflow-state bootstrap helpers used by `cf init`."""

from dataclasses import dataclass
from typing import Any

from ...config.models import (
    AgentSettings,
    CodingAgentSettings,
    HooksSettings,
    ObservabilitySettings,
    PollingSettings,
    ReviewSettings,
    ServerSettings,
    Settings,
    TrackerSettings,
    WorkspaceSettings,
)
from ...errors import TrackerClientError
from .bootstrap_queries import (
    CREATE_PROJECT_MUTATION,
    CREATE_WORKFLOW_STATE_MUTATION,
)
from .graphql import LinearGraphQLClient, RequestFunction
from .ops.ops_queries import PROJECTS_QUERY, TEAMS_QUERY
from .project_resolution import (
    PROJECT_LOOKUP_QUERY,
    project_ambiguous_error,
    validate_project_name,
)


@dataclass(frozen=True, slots=True)
class LinearBootstrapState:
    id: str
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class LinearBootstrapTeam:
    id: str
    name: str
    key: str
    states: tuple[LinearBootstrapState, ...]


@dataclass(frozen=True, slots=True)
class LinearBootstrapProject:
    id: str
    name: str
    slug_id: str
    teams: tuple[LinearBootstrapTeam, ...]
    url: str = ""


class LinearBootstrapper:
    """Resolve and provision Linear projects and team workflow states."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.linear.app/graphql",
        request_fun: RequestFunction | None = None,
    ) -> None:
        self._client = LinearGraphQLClient(
            _bootstrap_settings(api_key=api_key, endpoint=endpoint),
            request_fun=request_fun,
        )

    async def close(self) -> None:
        await self._client.close()

    async def resolve_project(self, reference: str) -> LinearBootstrapProject | None:
        project_name = validate_project_name(reference, config_error=False)
        response = await self._client.request(
            PROJECT_LOOKUP_QUERY,
            {"name": project_name, "first": 10},
        )
        nodes = _nodes(_data(response, "projects"))
        if not nodes:
            return None
        if len(nodes) > 1:
            raise project_ambiguous_error(project_name)
        return await self._project_with_team_states(nodes[0])

    async def resolve_team(self, reference: str) -> LinearBootstrapTeam:
        lowered = reference.strip().lower()
        matches = [
            node
            for node in await self._teams()
            if lowered
            in {
                str(node.get("id") or "").strip().lower(),
                str(node.get("name") or "").strip().lower(),
                str(node.get("key") or "").strip().lower(),
            }
        ]
        if len(matches) != 1:
            raise TrackerClientError(("tracker_not_found", reference))
        return _team_from_node(matches[0])

    async def create_project(
        self,
        *,
        name: str,
        team: LinearBootstrapTeam,
    ) -> LinearBootstrapProject:
        response = await self._client.request(
            CREATE_PROJECT_MUTATION,
            {"input": {"name": name, "teamIds": [team.id]}},
        )
        payload = _data(response, "projectCreate")
        if payload.get("success") is not True:
            raise TrackerClientError(
                ("tracker_operation_failed", "tracker project creation failed")
            )
        project_node = payload.get("project")
        if not isinstance(project_node, dict):
            raise TrackerClientError("linear_unknown_payload")
        return await self._project_with_team_states(project_node)

    async def ensure_states(
        self,
        *,
        team: LinearBootstrapTeam,
        required_states: tuple[tuple[str, str], ...],
    ) -> tuple[LinearBootstrapState, ...]:
        existing = {state.name.strip().lower() for state in team.states}
        created: list[LinearBootstrapState] = []
        for state_name, state_type in required_states:
            if state_name.strip().lower() in existing:
                continue
            response = await self._client.request(
                CREATE_WORKFLOW_STATE_MUTATION,
                {
                    "input": {
                        "name": state_name,
                        "type": state_type,
                        "teamId": team.id,
                        "color": "#6B7280",
                    }
                },
            )
            payload = _data(response, "workflowStateCreate")
            if payload.get("success") is not True:
                raise TrackerClientError(
                    (
                        "tracker_operation_failed",
                        "tracker workflow state creation failed",
                    )
                )
            state_node = payload.get("workflowState")
            if not isinstance(state_node, dict):
                raise TrackerClientError("linear_unknown_payload")
            state = _state_from_node(state_node)
            existing.add(state.name.strip().lower())
            created.append(state)
        return tuple(created)

    async def _projects(self) -> list[dict[str, Any]]:
        payload = _data(
            await self._client.request(PROJECTS_QUERY, {"first": 100}), "projects"
        )
        return _nodes(payload)

    async def _teams(self) -> list[dict[str, Any]]:
        payload = _data(
            await self._client.request(TEAMS_QUERY, {"first": 100}), "teams"
        )
        return _nodes(payload)

    async def _project_with_team_states(
        self, project_node: dict[str, Any]
    ) -> LinearBootstrapProject:
        teams_by_id = {
            str(team.get("id")): team for team in await self._teams() if team.get("id")
        }
        teams = [
            teams_by_id.get(str(team.get("id") or ""), team)
            for team in _nodes(project_node.get("teams") or {})
        ]
        return _project_from_node({**project_node, "teams": {"nodes": teams}})


def _bootstrap_settings(*, api_key: str, endpoint: str) -> Settings:
    return Settings(
        failure_state="Human Review",
        terminal_states=("Done",),
        tracker=TrackerSettings(
            kind="linear",
            endpoint=endpoint,
            api_key=api_key,
            project="bootstrap",
        ),
        polling=PollingSettings(),
        workspace=WorkspaceSettings(),
        agent=AgentSettings(),
        coding_agent=CodingAgentSettings(),
        hooks=HooksSettings(),
        observability=ObservabilitySettings(),
        server=ServerSettings(),
        review=ReviewSettings(),
    )


def _data(body: dict[str, Any], field: str) -> dict[str, Any]:
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        detail = "; ".join(
            str(error.get("message") or "unknown tracker error") for error in errors[:3]
        )
        raise TrackerClientError(("tracker_operation_failed", detail))
    payload = (body.get("data") or {}).get(field)
    if not isinstance(payload, dict):
        raise TrackerClientError("linear_unknown_payload")
    return payload


def _nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise TrackerClientError("linear_unknown_payload")
    return [node for node in nodes if isinstance(node, dict)]


def _project_from_node(node: dict[str, Any]) -> LinearBootstrapProject:
    return LinearBootstrapProject(
        id=str(node.get("id") or ""),
        name=str(node.get("name") or ""),
        slug_id=str(node.get("slugId") or ""),
        url=str(node.get("url") or ""),
        teams=tuple(_team_from_node(team) for team in _nodes(node.get("teams") or {})),
    )


def _team_from_node(node: dict[str, Any]) -> LinearBootstrapTeam:
    return LinearBootstrapTeam(
        id=str(node.get("id") or ""),
        name=str(node.get("name") or ""),
        key=str(node.get("key") or ""),
        states=tuple(
            _state_from_node(state) for state in _nodes(node.get("states") or {})
        ),
    )


def _state_from_node(node: dict[str, Any]) -> LinearBootstrapState:
    return LinearBootstrapState(
        id=str(node.get("id") or ""),
        name=str(node.get("name") or ""),
        type=str(node.get("type") or ""),
    )
