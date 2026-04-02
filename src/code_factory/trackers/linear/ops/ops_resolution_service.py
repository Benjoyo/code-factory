"""Resolution and mutation-input helpers for normalized Linear operations."""

from __future__ import annotations

from ....errors import TrackerClientError
from .ops_common import LinearOpsCommon
from .ops_queries import (
    CREATE_RELATION_MUTATION,
    LABELS_QUERY,
    UPDATE_ISSUE_MUTATION,
    USERS_QUERY,
)
from .ops_resolution import find_exact, require_single


class LinearOpsResolutionMixin(LinearOpsCommon):
    """Helpers for resolving state, team, label, and relation targets."""

    async def _issue_input(
        self,
        values: dict[str, object],
        *,
        team_node: dict | None,
        project_node: dict | None,
        issue_node: dict | None,
    ) -> dict:
        payload = {
            key: value
            for key, value in {
                "title": values.get("title"),
                "description": values.get("description"),
                "priority": values.get("priority"),
            }.items()
            if value is not None
        }
        if project_node is not None:
            payload["projectId"] = project_node.get("id")
        if team_node is not None:
            payload["teamId"] = team_node.get("id")
        if values.get("state") is not None:
            payload["stateId"] = await self._resolve_state_id(
                values.get("state"),
                issue_node=issue_node,
                team_node=team_node,
                project_node=project_node,
            )
        if values.get("assignee") is not None:
            payload["assigneeId"] = await self._resolve_user_id(str(values["assignee"]))
        if values.get("labels"):
            payload["labelIds"] = await self._resolve_label_ids(
                self._string_list(values.get("labels"))
            )
        return payload

    async def _apply_relations(
        self,
        issue_id: str | None,
        values: dict[str, object],
    ) -> None:
        if not issue_id:
            return
        for blocker in self._string_list(values.get("blocked_by")):
            await self._create_relation(str(blocker), issue_id, relation_type="blocks")
        for blocked in self._string_list(values.get("blocks")):
            await self._create_relation(issue_id, str(blocked), relation_type="blocks")
        for related in self._string_list(values.get("related_to")):
            await self._create_relation(issue_id, str(related), relation_type="related")

    async def _create_relation(
        self,
        source_issue: str,
        related_issue: str,
        *,
        relation_type: str,
    ) -> None:
        response = await self._graphql(
            CREATE_RELATION_MUTATION,
            {
                "input": {
                    "issueId": await self._resolve_issue_id(source_issue),
                    "relatedIssueId": await self._resolve_issue_id(related_issue),
                    "type": relation_type,
                }
            },
        )
        relation = (
            (self._data(response, "issueRelationCreate") or {})
            if isinstance(response, dict)
            else {}
        )
        if relation.get("success") is not True:
            raise TrackerClientError(
                ("tracker_operation_failed", "tracker relation update failed")
            )

    async def _resolve_issue_target(
        self,
        project: object,
        team: object,
    ) -> tuple[dict | None, dict | None]:
        project_node = (
            None if project is None else await self._project_node(str(project))
        )
        team_node = await self._resolve_team(
            team=str(team) if team is not None else None,
            project=str(project) if project is not None else None,
        )
        if project_node is None and self._settings.tracker.project:
            project_node = await self._project_node(self._settings.tracker.project)
        return team_node, project_node

    async def _resolve_team(
        self,
        *,
        team: str | None,
        project: str | None,
    ) -> dict | None:
        if team:
            return find_exact(await self._teams(), team, "id", "name", "key")
        project_name = project or self._settings.tracker.project
        if not project_name:
            return None
        project_node = await self._project_node(project_name)
        teams = (project_node.get("teams") or {}).get("nodes") or []
        return require_single(teams, project_name, field_name="team")

    async def _resolve_state_id(
        self,
        state: object,
        *,
        issue_node: dict | None,
        team_node: dict | None,
        project_node: dict | None,
    ) -> str:
        if issue_node is not None:
            team_node = await self._team_for_issue(issue_node)
        elif team_node is None and project_node is not None:
            team_node = require_single(
                (project_node.get("teams") or {}).get("nodes") or [],
                project_node.get("name") or "project",
                field_name="team",
            )
        if team_node is None:
            raise TrackerClientError(
                ("tracker_missing_field", "`state` requires a resolvable team")
            )
        team_node = await self._team_with_states(team_node)
        if team_node is None:
            raise TrackerClientError(("tracker_not_found", "team"))
        state_node = find_exact(
            (team_node.get("states") or {}).get("nodes") or [],
            str(state),
            "id",
            "name",
        )
        return str(state_node.get("id"))

    async def _resolve_label_ids(self, labels: list[str]) -> list[str]:
        nodes = (
            self._data(await self._graphql(LABELS_QUERY, {"first": 100}), "issueLabels")
            or {}
        ).get("nodes") or []
        return [
            str(find_exact(nodes, label, "id", "name").get("id")) for label in labels
        ]

    async def _resolve_user_id(self, user: str) -> str:
        nodes = (
            self._data(await self._graphql(USERS_QUERY, {"first": 100}), "users") or {}
        ).get("nodes") or []
        return str(
            find_exact(nodes, user, "id", "name", "displayName", "email").get("id")
        )

    def _string_list(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    async def _update_issue_state(
        self,
        issue_id: str,
        *,
        state_id: str,
    ) -> dict:
        response = await self._graphql(
            UPDATE_ISSUE_MUTATION,
            {"id": issue_id, "input": {"stateId": state_id}},
        )
        return (self._data(response, "issueUpdate") or {}).get("issue") or {}
