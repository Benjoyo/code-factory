"""`sync_workpad` dynamic tool definition and execution helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from ....workspace.paths import canonicalize, is_within
from .linear_errors import linear_error_payload
from .registry import ToolContext, dynamic_tool
from .results import ToolExecutionError, ToolResult

SYNC_WORKPAD_CREATE = (
    "mutation($issueId: String!, $body: String!) { "
    "commentCreate(input: { issueId: $issueId, body: $body }) "
    "{ success comment { id url } } }"
)
SYNC_WORKPAD_UPDATE = (
    "mutation($id: String!, $body: String!) { "
    "commentUpdate(id: $id, input: { body: $body }) { success comment { id url } } }"
)


class SyncWorkpadInput(BaseModel):
    """Validated input shape for syncing issue workpad comments."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(
        description='Linear issue identifier (e.g. "ENG-123") or internal UUID.'
    )
    file_path: str = Field(
        description="Path to a local markdown file whose contents become the comment body."
    )
    comment_id: str | None = Field(
        default=None,
        description="Existing comment ID to update. Omit to create a new comment.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_arguments(cls, arguments: Any) -> Any:
        """Validate the required arguments for syncing a workpad comment."""

        if not isinstance(arguments, dict):
            raise PydanticCustomError(
                "sync_workpad_invalid_arguments",
                "sync_workpad: `issue_id` and `file_path` are required",
            )
        issue_id = arguments.get("issue_id")
        file_path = arguments.get("file_path")
        comment_id = arguments.get("comment_id")
        if not isinstance(issue_id, str) or not issue_id:
            raise PydanticCustomError(
                "sync_workpad_issue_id",
                "sync_workpad: `issue_id` is required",
            )
        if not isinstance(file_path, str) or not file_path:
            raise PydanticCustomError(
                "sync_workpad_file_path",
                "sync_workpad: `file_path` is required",
            )
        normalized = dict(arguments)
        normalized["issue_id"] = issue_id
        normalized["file_path"] = file_path
        normalized["comment_id"] = (
            comment_id if isinstance(comment_id, str) and comment_id else None
        )
        return normalized


def sync_workpad_request(
    issue_id: str, comment_id: str | None, body: str
) -> tuple[str, dict[str, Any]]:
    """Choose the correct mutation shape for create vs. update."""

    if comment_id:
        return SYNC_WORKPAD_UPDATE, {"id": comment_id, "body": body}
    return SYNC_WORKPAD_CREATE, {"issueId": issue_id, "body": body}


def read_workpad_file(path: str, allowed_roots: tuple[str, ...]) -> str:
    """Read a workpad file while enforcing the configured workspace boundaries."""

    file_path = Path(path)
    if not file_path.is_absolute() and allowed_roots:
        file_path = Path(allowed_roots[0]) / file_path
    try:
        canonical_file = canonicalize(str(file_path))
    except OSError as exc:
        raise ToolExecutionError(
            _sync_workpad_error(f"cannot read `{path}`: {exc}")
        ) from exc
    if allowed_roots and not any(
        is_within(root, canonical_file) for root in allowed_roots
    ):
        raise ToolExecutionError(
            _sync_workpad_error(f"`{path}` is outside the allowed workspace roots")
        )
    try:
        body = Path(canonical_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolExecutionError(
            _sync_workpad_error(f"cannot read `{path}`: {exc}")
        ) from exc
    if body == "":
        raise ToolExecutionError(_sync_workpad_error(f"file is empty: `{path}`"))
    return body


def _sync_workpad_error(message: str) -> dict[str, Any]:
    return {"error": {"message": f"sync_workpad: {message}"}}


def _validation_error_message(reason: ValidationError) -> str:
    error = reason.errors()[0]
    field = ".".join(str(item) for item in error.get("loc", ()))
    if error.get("type") == "extra_forbidden" and field:
        return f"unexpected field: `{field}`"
    if field:
        return f"invalid `{field}`"
    return "invalid arguments"


@dynamic_tool(args_model=SyncWorkpadInput)
async def sync_workpad(context: ToolContext, arguments: SyncWorkpadInput) -> ToolResult:
    """Create or update a workpad comment on a Linear issue. Reads the body from a local file to keep the conversation context small."""

    body = read_workpad_file(arguments.file_path, context.allowed_roots)
    query, variables = sync_workpad_request(
        arguments.issue_id, arguments.comment_id, body
    )
    try:
        response = await context.linear_client(query, variables)
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(response)
