from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ....errors import TrackerClientError
from ....workspace.paths import canonicalize, is_within
from .specs import LINEAR_GRAPHQL_TOOL, SYNC_WORKPAD_TOOL, supported_tool_names

SYNC_WORKPAD_CREATE = (
    "mutation($issueId: String!, $body: String!) { "
    "commentCreate(input: { issueId: $issueId, body: $body }) { success comment { id url } } }"
)
SYNC_WORKPAD_UPDATE = (
    "mutation($id: String!, $body: String!) { "
    "commentUpdate(id: $id, input: { body: $body }) { success comment { id url } } }"
)


class DynamicToolExecutor:
    def __init__(
        self,
        linear_client: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        allowed_roots: tuple[str, ...] = (),
    ) -> None:
        self._linear_client = linear_client
        self._allowed_roots = allowed_roots

    async def execute(
        self, tool: str | None, arguments: Any
    ) -> tuple[dict[str, Any], str]:
        if tool == LINEAR_GRAPHQL_TOOL:
            return await self._execute_linear_graphql(arguments), "tool_call_completed"
        if tool == SYNC_WORKPAD_TOOL:
            return await self._execute_sync_workpad(arguments), "tool_call_completed"
        return failure_response(
            {
                "error": {
                    "message": f"Unsupported dynamic tool: {tool!r}.",
                    "supportedTools": supported_tool_names(),
                }
            }
        ), "unsupported_tool_call"

    async def _execute_linear_graphql(self, arguments: Any) -> dict[str, Any]:
        try:
            query, variables = normalize_linear_graphql_arguments(arguments)
            return graphql_response(await self._linear_client(query, variables))
        except Exception as exc:
            return failure_response(tool_error_payload(exc))

    async def _execute_sync_workpad(self, arguments: Any) -> dict[str, Any]:
        try:
            issue_id, file_path, comment_id = normalize_sync_workpad_args(arguments)
            body = read_workpad_file(file_path, self._allowed_roots)
            query, variables = sync_workpad_request(issue_id, comment_id, body)
            return graphql_response(await self._linear_client(query, variables))
        except Exception as exc:
            return failure_response(tool_error_payload(exc))


def normalize_linear_graphql_arguments(arguments: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(arguments, str):
        query = arguments.strip()
        if not query:
            raise ValueError("missing_query")
        return query, {}
    if isinstance(arguments, dict):
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("missing_query")
        variables = arguments.get("variables") or {}
        if not isinstance(variables, dict):
            raise TypeError("invalid_variables")
        return query.strip(), variables
    raise TypeError("invalid_arguments")


def normalize_sync_workpad_args(arguments: Any) -> tuple[str, str, str | None]:
    if not isinstance(arguments, dict):
        raise ValueError(("sync_workpad", "`issue_id` and `file_path` are required"))
    issue_id = arguments.get("issue_id")
    file_path = arguments.get("file_path")
    comment_id = arguments.get("comment_id")
    if not isinstance(issue_id, str) or not issue_id:
        raise ValueError(("sync_workpad", "`issue_id` is required"))
    if not isinstance(file_path, str) or not file_path:
        raise ValueError(("sync_workpad", "`file_path` is required"))
    return (
        issue_id,
        file_path,
        comment_id if isinstance(comment_id, str) and comment_id else None,
    )


def sync_workpad_request(
    issue_id: str, comment_id: str | None, body: str
) -> tuple[str, dict[str, Any]]:
    if comment_id:
        return SYNC_WORKPAD_UPDATE, {"id": comment_id, "body": body}
    return SYNC_WORKPAD_CREATE, {"issueId": issue_id, "body": body}


def read_workpad_file(path: str, allowed_roots: tuple[str, ...]) -> str:
    file_path = Path(path)
    if not file_path.is_absolute() and allowed_roots:
        file_path = Path(allowed_roots[0]) / file_path
    try:
        canonical_file = canonicalize(str(file_path))
    except OSError as exc:
        raise ValueError(("sync_workpad", f"cannot read `{path}`: {exc}")) from exc
    if allowed_roots and not any(
        is_within(root, canonical_file) for root in allowed_roots
    ):
        raise ValueError(
            ("sync_workpad", f"`{path}` is outside the allowed workspace roots")
        )
    try:
        body = Path(canonical_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(("sync_workpad", f"cannot read `{path}`: {exc}")) from exc
    if body == "":
        raise ValueError(("sync_workpad", f"file is empty: `{path}`"))
    return body


def graphql_response(response: Any) -> dict[str, Any]:
    success = not (
        isinstance(response, dict)
        and isinstance(response.get("errors"), list)
        and response["errors"]
    )
    return {
        "success": success,
        "contentItems": [{"type": "inputText", "text": encode_payload(response)}],
    }


def failure_response(payload: Any) -> dict[str, Any]:
    return {
        "success": False,
        "contentItems": [{"type": "inputText", "text": encode_payload(payload)}],
    }


def encode_payload(payload: Any) -> str:
    return (
        json.dumps(payload, indent=2, sort_keys=True)
        if isinstance(payload, dict | list)
        else repr(payload)
    )


def tool_error_payload(reason: Exception) -> dict[str, Any]:
    normalized = (
        reason.reason if isinstance(reason, TrackerClientError) else str(reason)
    )
    if isinstance(reason, ValueError) and reason.args:
        payload = reason.args[0]
        if payload == "missing_query":
            return {
                "error": {
                    "message": "`linear_graphql` requires a non-empty `query` string."
                }
            }
        if isinstance(payload, tuple) and payload[0] == "sync_workpad":
            return {"error": {"message": f"sync_workpad: {payload[1]}"}}
    if (
        isinstance(reason, TypeError)
        and reason.args
        and reason.args[0] == "invalid_arguments"
    ):
        return {
            "error": {
                "message": "`linear_graphql` expects either a GraphQL query string or an object with `query` and optional `variables`."
            }
        }
    if (
        isinstance(reason, TypeError)
        and reason.args
        and reason.args[0] == "invalid_variables"
    ):
        return {
            "error": {
                "message": "`linear_graphql.variables` must be a JSON object when provided."
            }
        }
    if normalized == "missing_linear_api_token":
        return {
            "error": {
                "message": "Code Factory is missing Linear auth. Set `linear.api_key` in `WORKFLOW.md` or export `LINEAR_API_KEY`."
            }
        }
    if (
        isinstance(normalized, tuple)
        and len(normalized) == 2
        and normalized[0] == "linear_api_status"
    ):
        return {
            "error": {
                "message": f"Linear GraphQL request failed with HTTP {normalized[1]}.",
                "status": normalized[1],
            }
        }
    if (
        isinstance(normalized, tuple)
        and len(normalized) == 2
        and normalized[0] == "linear_api_request"
    ):
        return {
            "error": {
                "message": "Linear GraphQL request failed before receiving a successful response.",
                "reason": repr(normalized[1]),
            }
        }
    return {
        "error": {
            "message": "Linear GraphQL tool execution failed.",
            "reason": repr(normalized),
        }
    }
