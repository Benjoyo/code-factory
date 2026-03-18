from __future__ import annotations

import asyncio
import logging

from ..config.models import Settings
from ..errors import WorkspaceError
from ..runtime.subprocess import ProcessTree

LOGGER = logging.getLogger(__name__)


async def run_hook(
    settings: Settings,
    command: str,
    workspace: str,
    issue_context: dict[str, str | None],
    hook_name: str,
    *,
    fatal: bool,
) -> None:
    LOGGER.info(
        "Running workspace hook hook=%s issue_id=%s issue_identifier=%s workspace=%s",
        hook_name,
        issue_context["issue_id"] or "n/a",
        issue_context["issue_identifier"],
        workspace,
    )

    process = await ProcessTree.spawn_shell(
        command,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        status, output = await process.capture_output(settings.hooks.timeout_ms)
    except TimeoutError as exc:
        await process.terminate()
        LOGGER.warning(
            "Workspace hook timed out hook=%s issue_identifier=%s workspace=%s timeout_ms=%s",
            hook_name,
            issue_context["issue_identifier"],
            workspace,
            settings.hooks.timeout_ms,
        )
        raise WorkspaceError(
            ("workspace_hook_timeout", hook_name, settings.hooks.timeout_ms)
        ) from exc

    if status == 0:
        return

    LOGGER.warning(
        "Workspace hook failed\nhook=%s\nissue_identifier=%s\nworkspace=%s\nstatus=%s\noutput:\n%s",
        hook_name,
        issue_context["issue_identifier"],
        workspace,
        status,
        output.rstrip() or "<no output>",
    )
    if fatal:
        raise WorkspaceError(("workspace_hook_failed", hook_name, status, output))
    raise WorkspaceError(("workspace_hook_failed", hook_name, status, output))
