"""Shared support primitives for normalized Linear ticket operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Self

from ....config.models import Settings
from ....errors import TrackerClientError
from ..graphql import LinearGraphQLClient
from ..project_resolution import (
    PROJECT_LOOKUP_QUERY,
    project_ambiguous_error,
    project_not_found_error,
    validate_project_name,
)
from .ops_queries import ISSUE_QUERY, PROJECTS_QUERY, TEAMS_QUERY
from .ops_resolution import find_optional


class LinearOpsCommon:
    """Base helpers shared across read/write Linear operation mixins."""

    def __init__(
        self,
        settings: Settings,
        graphql: Callable[[str, dict], Awaitable[dict]],
        *,
        allowed_roots: tuple[str, ...] = (),
        closer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._graphql = graphql
        self._allowed_roots = allowed_roots
        self._closer = closer
        self._project_cache: dict[str, dict] = {}

    @classmethod
    def from_settings(
        cls, settings: Settings, *, allowed_roots: tuple[str, ...] = ()
    ) -> Self:
        client = LinearGraphQLClient(settings)
        return cls(
            settings,
            client.request,
            allowed_roots=allowed_roots,
            closer=client.close,
        )

    async def close(self) -> None:
        if self._closer is not None:
            await self._closer()

    async def raw_graphql(self, query: str, variables: dict | None = None) -> dict:
        return await self._graphql(query, variables or {})

    async def _issue_node(
        self,
        issue: str,
        *,
        include_description: bool,
        include_comments: bool,
        include_attachments: bool,
        include_relations: bool,
    ) -> dict:
        body = await self._graphql(
            ISSUE_QUERY,
            {
                "id": issue,
                "includeDescription": include_description,
                "includeComments": include_comments,
                "includeAttachments": include_attachments,
                "includeRelations": include_relations,
            },
        )
        issue_node = self._data(body, "issue")
        if not isinstance(issue_node, dict):
            raise TrackerClientError(("tracker_not_found", issue))
        return issue_node

    async def _resolve_issue_id(self, issue: str) -> str:
        return str(
            (
                await self._issue_node(
                    issue,
                    include_description=False,
                    include_comments=False,
                    include_attachments=False,
                    include_relations=False,
                )
            ).get("id")
        )

    async def _projects(self) -> list[dict]:
        return (
            self._data(await self._graphql(PROJECTS_QUERY, {"first": 100}), "projects")
            or {}
        ).get("nodes") or []

    async def _project_node(self, project: str) -> dict:
        project_name = validate_project_name(project, config_error=False)
        cached = self._project_cache.get(project_name.lower())
        if cached is not None:
            return cached
        payload = (
            self._data(
                await self._graphql(
                    PROJECT_LOOKUP_QUERY,
                    {"name": project_name, "first": 10},
                ),
                "projects",
            )
            or {}
        )
        nodes = [node for node in payload.get("nodes") or [] if isinstance(node, dict)]
        if not nodes:
            raise project_not_found_error(project_name)
        if len(nodes) > 1:
            raise project_ambiguous_error(project_name)
        self._project_cache[project_name.lower()] = nodes[0]
        return nodes[0]

    async def _teams(self) -> list[dict]:
        return (
            self._data(await self._graphql(TEAMS_QUERY, {"first": 100}), "teams") or {}
        ).get("nodes") or []

    async def _team_with_states(self, team_node: dict | None) -> dict | None:
        if not isinstance(team_node, dict):
            return None
        states = (team_node.get("states") or {}).get("nodes")
        if isinstance(states, list):
            return team_node
        team_id = str(team_node.get("id") or "")
        if not team_id:
            return team_node
        return find_optional(await self._teams(), team_id, "id") or team_node

    async def _projects_with_team_states(self) -> list[dict]:
        teams_by_id = {
            str(team.get("id")): team
            for team in await self._teams()
            if isinstance(team, dict) and team.get("id")
        }
        return [
            self._hydrate_project_teams(project, teams_by_id)
            for project in await self._projects()
        ]

    def _hydrate_project_teams(
        self,
        project_node: dict,
        teams_by_id: dict[str, dict],
    ) -> dict:
        teams = (project_node.get("teams") or {}).get("nodes") or []
        hydrated = [
            teams_by_id.get(str(team.get("id") or ""), team)
            for team in teams
            if isinstance(team, dict)
        ]
        return {**project_node, "teams": {"nodes": hydrated}}

    async def _team_for_issue(self, issue_node: dict) -> dict:
        team = issue_node.get("team")
        if isinstance(team, dict) and team.get("id"):
            full_team = find_optional(await self._teams(), str(team.get("id")), "id")
            if full_team is not None:
                return full_team
        raise TrackerClientError(("tracker_not_found", "team"))

    def _data(self, body: dict, field: str) -> dict | None:
        if body.get("errors"):
            raise TrackerClientError(
                ("tracker_operation_failed", self._error_message(body["errors"]))
            )
        return (body.get("data") or {}).get(field)

    def _matches_issue(
        self,
        node: dict,
        *,
        project: str | None,
        state: str | None,
        query: str | None,
    ) -> bool:
        if project:
            project_node = node.get("project") or {}
            if (project_node.get("id") or "").lower() != project.strip().lower():
                return False
        if (
            state
            and ((node.get("state") or {}).get("name") or "").strip().lower()
            != state.strip().lower()
        ):
            return False
        if query:
            lowered = query.strip().lower()
            haystacks = [
                node.get("identifier") or "",
                node.get("title") or "",
                node.get("description") or "",
            ]
            return any(lowered in str(value).lower() for value in haystacks)
        return True

    def _error_message(self, errors: list[dict]) -> str:
        return "; ".join(
            str(error.get("message") or "unknown tracker error") for error in errors[:3]
        )
