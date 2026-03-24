from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from ..config.models import Settings
from ..errors import WorkspaceError
from ..runtime.subprocess import ProcessTree

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HookCommandResult:
    status: int
    stdout: str
    stderr: str

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}{self.stderr}"


async def run_hook(
    settings: Settings,
    command: str,
    workspace: str,
    issue_context: dict[str, str | None],
    hook_name: str,
    *,
    fatal: bool,
) -> None:
    result = await run_hook_command(
        settings,
        command,
        workspace,
        issue_context,
        hook_name,
    )
    if result.status == 0:
        return

    LOGGER.warning(
        "Workspace hook failed\nhook=%s\nissue_identifier=%s\nworkspace=%s\nstatus=%s\noutput:\n%s",
        hook_name,
        issue_context["issue_identifier"],
        workspace,
        result.status,
        result.combined_output.rstrip() or "<no output>",
    )
    raise WorkspaceError(
        ("workspace_hook_failed", hook_name, result.status, result.combined_output)
    )


async def run_hook_command(
    settings: Settings,
    command: str,
    workspace: str,
    issue_context: dict[str, str | None],
    hook_name: str,
    *,
    env: dict[str, str | None] | None = None,
) -> HookCommandResult:
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
        env=_hook_environment(issue_context, env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        status, stdout, stderr = await process.capture_streams(
            settings.hooks.timeout_ms
        )
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

    return HookCommandResult(status=status, stdout=stdout, stderr=stderr)


def _hook_environment(
    issue_context: dict[str, str | None],
    env: dict[str, str | None] | None,
) -> dict[str, str]:
    hook_env = dict(os.environ)
    if issue_context.get("issue_id") is not None:
        hook_env["CF_ISSUE_ID"] = str(issue_context["issue_id"])
    if issue_context.get("issue_identifier") is not None:
        hook_env["CF_ISSUE_IDENTIFIER"] = str(issue_context["issue_identifier"])
    if env:
        for key, value in env.items():
            if value is None:
                continue
            hook_env[key] = str(value)
    return hook_env
