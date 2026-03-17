from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProcessTree:
    process: asyncio.subprocess.Process
    command: str
    cwd: str

    @classmethod
    async def spawn_shell(
        cls,
        command: str,
        *,
        cwd: str,
        stdin: Any = None,
        stdout: Any = asyncio.subprocess.PIPE,
        stderr: Any = asyncio.subprocess.PIPE,
    ) -> ProcessTree:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "stdin": stdin,
            "stdout": stdout,
            "stderr": stderr,
        }
        if os.name == "nt":  # pragma: no cover - Windows-only process flags
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_shell(command, **kwargs)
        return cls(process=process, command=command, cwd=cwd)

    @property
    def pid(self) -> int | None:
        return self.process.pid

    async def wait(self) -> int:
        return await self.process.wait()

    async def terminate(self, grace_ms: int = 3_000) -> None:
        if self.process.returncode is not None:
            return

        with contextlib.suppress(ProcessLookupError, PermissionError):
            if os.name == "nt":  # pragma: no cover - Windows-only process signaling
                self.process.terminate()
            elif (
                self.process.pid is not None
            ):  # pragma: no branch - pid can disappear during teardown
                os.killpg(self.process.pid, signal.SIGTERM)

        try:
            await asyncio.wait_for(self.process.wait(), grace_ms / 1000)
            return
        except TimeoutError:
            pass
        except (
            ProcessLookupError,
            PermissionError,
        ):  # pragma: no cover - process exited mid-shutdown
            return

        with contextlib.suppress(ProcessLookupError, PermissionError):
            if os.name == "nt":  # pragma: no cover - Windows-only process signaling
                self.process.kill()
            elif (
                self.process.pid is not None
            ):  # pragma: no branch - pid can disappear during teardown
                os.killpg(self.process.pid, signal.SIGKILL)

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.process.wait(), 2)

    async def communicate(self, timeout_ms: int | None = None) -> tuple[bytes, bytes]:
        if timeout_ms is None:
            return await self.process.communicate()
        return await asyncio.wait_for(self.process.communicate(), timeout_ms / 1000)

    async def capture_output(self, timeout_ms: int) -> tuple[int, str]:
        stdout, stderr = await self.communicate(timeout_ms)
        output = b"".join(part for part in (stdout, stderr) if part)
        return self.process.returncode or 0, output.decode("utf-8", errors="replace")
