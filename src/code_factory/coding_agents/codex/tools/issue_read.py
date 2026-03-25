"""Read-only dynamic tools for current-ticket-oriented tracker access."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .linear_errors import linear_error_payload
from .registry import ToolContext, dynamic_tool
from .results import ToolExecutionError, ToolResult
from .tracker_context import resolve_issue, resolve_project


class TrackerIssueGetInput(BaseModel):
    """Input schema for `tracker_issue_get`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    issue: str | None = Field(default=None)
    include_comments: bool = False
    include_attachments: bool = False


class TrackerStatesInput(BaseModel):
    """Input schema for `tracker_states`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    issue: str | None = Field(default=None)


class TrackerIssueSearchInput(BaseModel):
    """Input schema for `tracker_issue_search`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    query: str | None = Field(default=None)
    state: str | None = Field(default=None)
    limit: int = Field(default=20, ge=1, le=100)


@dynamic_tool(args_model=TrackerIssueGetInput)
async def tracker_issue_get(
    context: ToolContext, arguments: TrackerIssueGetInput
) -> ToolResult:
    """Read one tracker issue, defaulting to the current ticket when `issue` is omitted."""

    try:
        payload = await context.tracker_ops.read_issue(
            resolve_issue(context, arguments.issue),
            include_description=True,
            include_comments=arguments.include_comments,
            include_attachments=arguments.include_attachments,
            include_relations=True,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)


@dynamic_tool(args_model=TrackerStatesInput)
async def tracker_states(
    context: ToolContext, arguments: TrackerStatesInput
) -> ToolResult:
    """Read tracker workflow states for one issue, defaulting to the current ticket."""

    try:
        payload = await context.tracker_ops.read_states(
            issue=resolve_issue(context, arguments.issue),
            team=None,
            project=None,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)


@dynamic_tool(args_model=TrackerIssueSearchInput)
async def tracker_issue_search(
    context: ToolContext, arguments: TrackerIssueSearchInput
) -> ToolResult:
    """Search issue summaries in the current workflow project without expanding heavy issue context."""

    try:
        payload = await context.tracker_ops.read_issues(
            project=resolve_project(context),
            state=arguments.state,
            query=arguments.query,
            limit=arguments.limit,
            include_description=False,
            include_comments=False,
            include_attachments=False,
            include_relations=False,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)
