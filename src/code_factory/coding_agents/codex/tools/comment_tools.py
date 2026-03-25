"""Comment-oriented dynamic tools for tracker access."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .linear_errors import linear_error_payload
from .registry import ToolContext, dynamic_tool
from .results import ToolExecutionError, ToolResult
from .tracker_context import resolve_issue


class TrackerCommentCreateInput(BaseModel):
    """Input schema for `tracker_comment_create`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    body: str = Field(min_length=1)
    issue: str | None = None


class TrackerCommentUpdateInput(BaseModel):
    """Input schema for `tracker_comment_update`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    comment_id: str = Field(min_length=1)
    body: str = Field(min_length=1)


@dynamic_tool(args_model=TrackerCommentCreateInput)
async def tracker_comment_create(
    context: ToolContext, arguments: TrackerCommentCreateInput
) -> ToolResult:
    """Create a tracker comment, defaulting to the current ticket when `issue` is omitted."""

    try:
        payload = await context.tracker_ops.create_comment(
            resolve_issue(context, arguments.issue),
            arguments.body,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)


@dynamic_tool(args_model=TrackerCommentUpdateInput)
async def tracker_comment_update(
    context: ToolContext, arguments: TrackerCommentUpdateInput
) -> ToolResult:
    """Update a tracker comment by comment identifier."""

    try:
        payload = await context.tracker_ops.update_comment(
            arguments.comment_id,
            arguments.body,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)
