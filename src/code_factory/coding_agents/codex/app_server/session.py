from __future__ import annotations

"""Tracks the runtime metadata for one live Codex app-server session."""

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from ....errors import AppServerError
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
    event_queue: asyncio.Queue[tuple[str, Any]]
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]
    wait_task: asyncio.Task[None]
    pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict
    )
    routing_task: asyncio.Task[None] | None = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request_id: int = 3
    current_turn_id: str | None = None
    stopping: bool = False

    @property
    def runtime_pid(self) -> str | None:
        return str(self.process_tree.pid) if self.process_tree.pid is not None else None

    def reserve_request_id(self) -> int:
        request_id = self.next_request_id
        self.next_request_id += 1
        return request_id

    async def stop(self) -> None:
        """Shut down the subprocess and cancel background readers cleanly."""
        self.stopping = True
        await self.process_tree.terminate()
        exit_code = self.process_tree.process.returncode or 0
        await self.stdout_queue.put(("exit", exit_code))
        await self.event_queue.put(("exit", exit_code))
        error = AppServerError(("port_exit", exit_code))
        for request_id in list(self.pending_requests):
            future = self.pending_requests.pop(request_id)
            if not future.done():
                future.set_exception(error)
        for task in (
            self.routing_task,
            self.stdout_task,
            self.stderr_task,
            self.wait_task,
        ):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
