from __future__ import annotations

"""Tracks the runtime metadata for one live Codex app-server session."""

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from ....runtime.subprocess import ProcessTree


@dataclass(slots=True)
class AppServerSession:
    """Immutable session record that DTOs the App Server runtime state."""

    process_tree: ProcessTree
    workspace: str
    approval_policy: str | dict[str, Any]
    thread_sandbox: str
    turn_sandbox_policy: dict[str, Any]
    thread_id: str
    read_timeout_ms: int
    turn_timeout_ms: int
    auto_approve_requests: bool
    stdout_queue: asyncio.Queue[tuple[str, Any]]
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]
    wait_task: asyncio.Task[None]
    stopping: bool = False

    @property
    def runtime_pid(self) -> str | None:
        return str(self.process_tree.pid) if self.process_tree.pid is not None else None

    async def stop(self) -> None:
        """Shut down the subprocess and cancel background readers cleanly."""
        self.stopping = True
        await self.process_tree.terminate()
        await self.stdout_queue.put(("exit", self.process_tree.process.returncode or 0))
        for task in (self.stdout_task, self.stderr_task, self.wait_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
