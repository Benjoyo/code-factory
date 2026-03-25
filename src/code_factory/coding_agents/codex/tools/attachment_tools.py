"""Attachment-oriented dynamic tools for tracker access."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .linear_errors import linear_error_payload
from .registry import ToolContext, dynamic_tool
from .results import ToolExecutionError, ToolResult
from .tracker_context import resolve_issue


class TrackerPrLinkInput(BaseModel):
    """Input schema for `tracker_pr_link`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    url: str = Field(min_length=1)
    title: str | None = None
    issue: str | None = None


class TrackerFileUploadInput(BaseModel):
    """Input schema for `tracker_file_upload`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    file_path: str = Field(min_length=1)


@dynamic_tool(args_model=TrackerPrLinkInput)
async def tracker_pr_link(
    context: ToolContext, arguments: TrackerPrLinkInput
) -> ToolResult:
    """Attach a PR link to a tracker issue, defaulting to the current ticket when `issue` is omitted."""

    try:
        payload = await context.tracker_ops.link_pr(
            resolve_issue(context, arguments.issue),
            arguments.url,
            arguments.title,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)


@dynamic_tool(args_model=TrackerFileUploadInput)
async def tracker_file_upload(
    context: ToolContext, arguments: TrackerFileUploadInput
) -> ToolResult:
    """Upload a file from the workspace and return the tracker asset metadata."""

    try:
        payload = await context.tracker_ops.upload_file(arguments.file_path)
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)
