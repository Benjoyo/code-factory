from __future__ import annotations

"""Thin bridge that keeps the orchestrator decoupled from Linear's GraphQL schema."""

from collections.abc import Callable
from typing import Any

from ...config.models import Settings
from ...errors import TrackerClientError
from ...issues import Issue, IssueComment
from .decoding import (
    assignee_id,
    decode_comments_page_response,
    decode_linear_page_response,
    decode_linear_response,
    next_page_cursor,
)
from .graphql import LinearGraphQLClient, RequestFunction
from .queries import (
    COMMENTS_QUERY,
    CREATE_COMMENT_MUTATION,
    QUERY,
    QUERY_BY_IDENTIFIER,
    QUERY_BY_IDS,
    STATE_LOOKUP_QUERY,
    UPDATE_COMMENT_MUTATION,
    UPDATE_STATE_MUTATION,
    VIEWER_QUERY,
)


def build_tracker(settings: Settings, **kwargs: Any) -> LinearClient:
    return LinearClient(settings, **kwargs)


class LinearClient:
    """GraphQL wrapper that enforces the expected tracker behaviors."""

    ISSUE_PAGE_SIZE = 50
    COMMENT_PAGE_SIZE = 50

    def __init__(
        self,
        settings: Settings,
        *,
        request_fun: RequestFunction | None = None,
        client_factory: Callable[[], LinearGraphQLClient] | None = None,
    ) -> None:
        self._settings = settings
        self._graphql_client = (
            client_factory()
            if client_factory
            else LinearGraphQLClient(settings, request_fun=request_fun)
        )

    async def close(self) -> None:
        await self._graphql_client.close()

    async def fetch_candidate_issues(self) -> list[Issue]:
        """Return a snapshot of the active states that match the configured assignee."""
        self._require_credentials()
        assignee_filter = await self._routing_assignee_filter()
        return await self._fetch_by_states(
            list(self._settings.tracker.active_states), assignee_filter
        )

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if not state_names:
            return []
        self._require_credentials()
        unique_states = list(dict.fromkeys(map(str, state_names)))
        return await self._fetch_by_states(unique_states, None)

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        ids = list(dict.fromkeys(issue_ids))
        if not ids:
            return []
        self._require_credentials()
        assignee_filter = await self._routing_assignee_filter()
        return await self._fetch_issue_states(ids, assignee_filter)

    async def fetch_issue_by_identifier(self, identifier: str) -> Issue | None:
        self._require_credentials()
        body = await self.graphql(
            QUERY_BY_IDENTIFIER,
            {
                "projectSlug": self._settings.tracker.project_slug,
                "identifier": identifier,
                "relationFirst": self.ISSUE_PAGE_SIZE,
            },
        )
        issues = decode_linear_response(body, None)
        if not issues:
            return None
        return issues[0]

    async def fetch_issue_comments(self, issue_id: str) -> list[IssueComment]:
        self._require_credentials()
        after_cursor: str | None = None
        comments: list[IssueComment] = []
        while True:
            body = await self.graphql(
                COMMENTS_QUERY,
                {
                    "issueId": issue_id,
                    "first": self.COMMENT_PAGE_SIZE,
                    "after": after_cursor,
                },
            )
            page_comments, page_info = decode_comments_page_response(body)
            comments.extend(page_comments)
            after_cursor = next_page_cursor(page_info)
            if after_cursor is None:
                return comments

    async def create_comment(self, issue_id: str, body: str) -> None:
        response = await self.graphql(
            CREATE_COMMENT_MUTATION, {"issueId": issue_id, "body": body}
        )
        if response.get("data", {}).get("commentCreate", {}).get("success") is not True:
            raise TrackerClientError("comment_create_failed")

    async def update_comment(self, comment_id: str, body: str) -> None:
        response = await self.graphql(
            UPDATE_COMMENT_MUTATION, {"commentId": comment_id, "body": body}
        )
        if response.get("data", {}).get("commentUpdate", {}).get("success") is not True:
            raise TrackerClientError("comment_update_failed")

    async def update_issue_state(self, issue_id: str, state_name: str) -> None:
        state_id = await self._resolve_state_id(issue_id, state_name)
        response = await self.graphql(
            UPDATE_STATE_MUTATION, {"issueId": issue_id, "stateId": state_id}
        )
        if response.get("data", {}).get("issueUpdate", {}).get("success") is not True:
            raise TrackerClientError("issue_update_failed")

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        """Wrap every GraphQL call with credential validation upstream of the client."""
        self._require_credentials()
        return await self._graphql_client.request(query, variables, operation_name)

    def _require_credentials(self) -> None:
        if not self._settings.tracker.api_key:
            raise TrackerClientError("missing_linear_api_token")
        if not self._settings.tracker.project_slug:
            raise TrackerClientError("missing_linear_project_slug")

    async def _fetch_by_states(
        self,
        state_names: list[str],
        assignee_filter: dict[str, Any] | None,
    ) -> list[Issue]:
        # Query in pages so the response can scale with large issue sets.
        after_cursor: str | None = None
        issues: list[Issue] = []
        while True:
            body = await self.graphql(
                QUERY,
                {
                    "projectSlug": self._settings.tracker.project_slug,
                    "stateNames": state_names,
                    "first": self.ISSUE_PAGE_SIZE,
                    "relationFirst": self.ISSUE_PAGE_SIZE,
                    "after": after_cursor,
                },
            )
            page_issues, page_info = decode_linear_page_response(body, assignee_filter)
            issues.extend(page_issues)
            after_cursor = next_page_cursor(page_info)
            if after_cursor is None:
                return issues

    async def _fetch_issue_states(
        self,
        issue_ids: list[str],
        assignee_filter: dict[str, Any] | None,
    ) -> list[Issue]:
        order_index = {issue_id: index for index, issue_id in enumerate(issue_ids)}
        issues: list[Issue] = []
        for offset in range(0, len(issue_ids), self.ISSUE_PAGE_SIZE):
            batch_ids = issue_ids[offset : offset + self.ISSUE_PAGE_SIZE]
            body = await self.graphql(
                QUERY_BY_IDS,
                {
                    "ids": batch_ids,
                    "first": len(batch_ids),
                    "relationFirst": self.ISSUE_PAGE_SIZE,
                },
            )
            issues.extend(decode_linear_response(body, assignee_filter))
        # Return issues in the same order as the caller-supplied IDs.
        return sorted(
            issues, key=lambda issue: order_index.get(issue.id or "", len(order_index))
        )

    async def _routing_assignee_filter(self) -> dict[str, Any] | None:
        assignee = self._settings.tracker.assignee
        if assignee is None or not assignee.strip():
            return None
        if assignee.strip() != "me":
            return {"configured_assignee": assignee, "match_values": {assignee.strip()}}

        body = await self.graphql(VIEWER_QUERY, {})
        viewer_id = assignee_id(body.get("data", {}).get("viewer") or {})
        if viewer_id is None:
            raise TrackerClientError("missing_linear_viewer_identity")
        # Resolving `me` to the viewer id ensures downstream filters compare ids.
        return {"configured_assignee": "me", "match_values": {viewer_id}}

    async def _resolve_state_id(self, issue_id: str, state_name: str) -> str:
        # Confirm the state exists in the issue's workspace before hitting Linear.
        body = await self.graphql(
            STATE_LOOKUP_QUERY, {"issueId": issue_id, "stateName": state_name}
        )
        state_id = (
            body.get("data", {})
            .get("issue", {})
            .get("team", {})
            .get("states", {})
            .get("nodes", [{}])[0]
            .get("id")
        )
        if not isinstance(state_id, str):
            raise TrackerClientError("state_not_found")
        return state_id
