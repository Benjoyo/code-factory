"""Read-side normalized Linear ticket operations."""

from __future__ import annotations

from ....errors import TrackerClientError
from .ops_normalize import (
    normalize_issue,
    normalize_project,
    normalize_team,
)
from .ops_queries import ISSUES_QUERY
from .ops_resolution import find_exact
from .ops_resolution_service import LinearOpsResolutionMixin


class LinearOpsReadMixin(LinearOpsResolutionMixin):
    """Issue, project, state, comment, and workpad read operations."""

    async def read_issue(
        self,
        issue: str,
        *,
        include_description: bool,
        include_comments: bool,
        include_attachments: bool,
        include_relations: bool,
    ) -> dict:
        node = await self._issue_node(
            issue,
            include_description=include_description,
            include_comments=include_comments,
            include_attachments=include_attachments,
            include_relations=include_relations,
        )
        return {
            "issue": normalize_issue(
                node,
                include_description=include_description,
                include_comments=include_comments,
                include_attachments=include_attachments,
                include_relations=include_relations,
            )
        }

    async def read_issues(
        self,
        *,
        project: str | None,
        state: str | None,
        query: str | None,
        limit: int,
        include_description: bool,
        include_comments: bool,
        include_attachments: bool,
        include_relations: bool,
    ) -> dict:
        project_filter = project or self._settings.tracker.project_slug
        issues: list[dict] = []
        after: str | None = None
        while len(issues) < limit:
            body = await self._graphql(
                ISSUES_QUERY,
                {
                    "first": max(limit * 3, 50),
                    "after": after,
                    "includeDescription": include_description,
                    "includeComments": include_comments,
                    "includeAttachments": include_attachments,
                    "includeRelations": include_relations,
                },
            )
            issue_page = (
                (self._data(body, "issues") or {}) if isinstance(body, dict) else {}
            )
            nodes = issue_page.get("nodes") or []
            for node in nodes:
                if not self._matches_issue(
                    node,
                    project=project_filter,
                    state=state,
                    query=query,
                ):
                    continue
                issues.append(
                    normalize_issue(
                        node,
                        include_description=include_description,
                        include_comments=include_comments,
                        include_attachments=include_attachments,
                        include_relations=include_relations,
                    )
                )
                if len(issues) >= limit:
                    break
            after = self._next_page_cursor(issue_page)
            if after is None:
                break
        return {"issues": issues, "count": len(issues)}

    async def read_project(self, project: str) -> dict:
        node = find_exact(await self._projects(), project, "id", "name", "slugId")
        return {"project": normalize_project(node, include_teams=True)}

    async def read_projects(self, *, query: str | None, limit: int) -> dict:
        projects = await self._projects()
        if query:
            lowered = query.strip().lower()
            projects = [
                node
                for node in projects
                if lowered in (node.get("name") or "").lower()
                or lowered in (node.get("slugId") or "").lower()
            ]
        normalized = [
            normalize_project(node, include_teams=True) for node in projects[:limit]
        ]
        return {"projects": normalized, "count": len(normalized)}

    async def read_states(
        self,
        *,
        issue: str | None,
        team: str | None,
        project: str | None,
    ) -> dict:
        if issue:
            issue_payload = await self.read_issue(
                issue,
                include_description=False,
                include_comments=False,
                include_attachments=False,
                include_relations=False,
            )
            issue_node = await self._issue_node(
                issue,
                include_description=False,
                include_comments=False,
                include_attachments=False,
                include_relations=False,
            )
            team_node = await self._team_for_issue(issue_node)
            normalized_with_states = normalize_team(team_node, include_states=True)
            if normalized_with_states is None:
                raise TrackerClientError(("tracker_not_found", "team"))
            return {
                "issue": issue_payload["issue"],
                "states": normalized_with_states["states"],
            }
        team_node = await self._resolve_team(team=team, project=project)
        if team_node is None:
            raise TrackerClientError(
                (
                    "tracker_missing_field",
                    "one of `issue`, `team`, or a default workflow project is required",
                )
            )
        normalized_team = normalize_team(team_node, include_states=False)
        normalized_with_states = normalize_team(team_node, include_states=True)
        if normalized_team is None or normalized_with_states is None:
            raise TrackerClientError(("tracker_not_found", "team"))
        return {"team": normalized_team, "states": normalized_with_states["states"]}

    async def list_comments(self, issue: str) -> dict:
        issue_node = await self._issue_node(
            issue,
            include_description=False,
            include_comments=True,
            include_attachments=False,
            include_relations=False,
        )
        comments = normalize_issue(
            issue_node,
            include_description=False,
            include_comments=True,
            include_attachments=False,
            include_relations=False,
        ).get("comments", [])
        return {"comments": comments, "count": len(comments)}

    def _next_page_cursor(self, issue_page: dict) -> str | None:
        page_info = issue_page.get("pageInfo") or {}
        has_next_page = page_info.get("hasNextPage") is True
        end_cursor = page_info.get("endCursor")
        if not has_next_page:
            return None
        if not isinstance(end_cursor, str) or not end_cursor:
            raise TrackerClientError(
                ("tracker_operation_failed", "tracker issues page missing end cursor")
            )
        return end_cursor

    async def get_workpad(self, issue: str) -> dict:
        payload = await self.list_comments(issue)
        for comment in payload["comments"]:
            if comment["resolved_at"] is None and (comment["body"] or "").startswith(
                "## Codex Workpad"
            ):
                return {
                    "found": True,
                    "comment_id": comment["id"],
                    "url": None,
                    "body": comment["body"],
                    "created_at": comment["created_at"],
                    "updated_at": comment["updated_at"],
                }
        return {
            "found": False,
            "comment_id": None,
            "url": None,
            "body": None,
            "created_at": None,
            "updated_at": None,
        }
