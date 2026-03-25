"""Current-ticket workpad dynamic tool."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ....workspace.workpad import WORKPAD_FILENAME
from .linear_errors import linear_error_payload
from .registry import ToolContext, dynamic_tool
from .results import ToolExecutionError, ToolResult
from .tracker_context import resolve_issue


class WorkpadSyncInput(BaseModel):
    """Input schema for `workpad_sync`."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"additionalProperties": False}
    )


@dynamic_tool(args_model=WorkpadSyncInput)
async def workpad_sync(
    context: ToolContext, _arguments: WorkpadSyncInput
) -> ToolResult:
    """Sync the current ticket's hydrated `workpad.md` file to the tracker workpad comment."""

    try:
        payload = await context.tracker_ops.sync_workpad(
            resolve_issue(context, None),
            file_path=WORKPAD_FILENAME,
        )
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    return ToolResult.ok(payload)
