"""Issue mutation dynamic tools for current-ticket-oriented tracker access."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from .linear_errors import linear_error_payload
from .registry import ToolContext, dynamic_tool
from .results import ToolExecutionError, ToolResult
from .tracker_context import resolve_issue, resolve_project


class TrackerIssueCreateInput(BaseModel):
    """Input schema for `tracker_issue_create`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    title: str = Field(min_length=1)
    description: str | None = None
    priority: int | None = Field(default=None, ge=0, le=4)
    assignee: str | None = None
    labels: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)


class TrackerIssueUpdateInput(BaseModel):
    """Input schema for `tracker_issue_update`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )

    issue: str | None = None
    title: str | None = None
    description: str | None = None
    priority: int | None = Field(default=None, ge=0, le=4)
    assignee: str | None = None
    labels: list[str] | None = None
    blocked_by: list[str] | None = None

    @model_validator(mode="after")
    def validate_mutation(self) -> TrackerIssueUpdateInput:
        if any(
            value is not None
            for value in (
                self.title,
                self.description,
                self.priority,
                self.assignee,
                self.labels,
                self.blocked_by,
            )
        ):
            return self
        raise PydanticCustomError(
            "tracker_issue_update_fields",
            "tracker_issue_update: at least one update field is required",
        )


@dynamic_tool(args_model=TrackerIssueCreateInput)
async def tracker_issue_create(
    context: ToolContext, arguments: TrackerIssueCreateInput
) -> ToolResult:
    """Create a tracker issue in the current workflow project."""

    try:
        payload = await context.tracker_ops.create_issue(
            title=arguments.title,
            description=arguments.description,
            project=resolve_project(context),
            team=None,
            state=None,
            priority=arguments.priority,
            assignee=arguments.assignee,
            labels=arguments.labels,
            blocked_by=arguments.blocked_by,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)


@dynamic_tool(args_model=TrackerIssueUpdateInput)
async def tracker_issue_update(
    context: ToolContext, arguments: TrackerIssueUpdateInput
) -> ToolResult:
    """Update a tracker issue, defaulting to the current ticket when `issue` is omitted."""

    try:
        payload = await context.tracker_ops.update_issue(
            resolve_issue(context, arguments.issue),
            title=arguments.title,
            description=arguments.description,
            project=None,
            team=None,
            state=None,
            priority=arguments.priority,
            assignee=arguments.assignee,
            labels=arguments.labels,
            blocked_by=arguments.blocked_by,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)
