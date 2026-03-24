"""Small shell helpers shared by review target resolution and launch flows."""

from __future__ import annotations

from dataclasses import dataclass

from ..runtime.subprocess import ProcessTree


@dataclass(frozen=True, slots=True)
class ShellResult:
    status: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return f"{self.stdout}{self.stderr}".strip()


async def capture_shell(
    command: str,
    *,
    cwd: str,
    env: dict[str, str] | None = None,
) -> ShellResult:
    process = await ProcessTree.spawn_shell(command, cwd=cwd, env=env)
    status, stdout, stderr = await process.capture_streams(timeout_ms=60_000)
    return ShellResult(status=status, stdout=stdout, stderr=stderr)
