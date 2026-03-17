from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ....runtime.subprocess import ProcessTree

LOGGER = logging.getLogger(__name__)
MAX_STREAM_LOG_BYTES = 1000


async def stdout_reader(
    stream: asyncio.StreamReader,
    queue: asyncio.Queue[tuple[str, Any]],
) -> None:
    pending = ""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            if pending:
                await queue.put(("line", pending))
            return
        pending += chunk.decode("utf-8", errors="replace")
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            await queue.put(("line", line.rstrip("\r")))


async def stderr_reader(stream: asyncio.StreamReader) -> None:
    pending = ""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            if pending.strip():
                log_non_json_stream_line(pending, "stderr")
            return
        pending += chunk.decode("utf-8", errors="replace")
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            log_non_json_stream_line(line.rstrip("\r"), "stderr")


async def wait_for_exit(
    process_tree: ProcessTree, queue: asyncio.Queue[tuple[str, Any]]
) -> None:
    await queue.put(("exit", await process_tree.wait()))


async def send_message(process_tree: ProcessTree, message: dict[str, Any]) -> None:
    assert process_tree.process.stdin is not None
    process_tree.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    await process_tree.process.stdin.drain()


def log_non_json_stream_line(data: str, stream_label: str) -> None:
    text = data.strip()[:MAX_STREAM_LOG_BYTES]
    if not text:
        return
    if any(
        token in text.lower()
        for token in (
            "error",
            "warn",
            "warning",
            "failed",
            "fatal",
            "panic",
            "exception",
        )
    ):
        LOGGER.warning("Codex %s output: %s", stream_label, text)
    else:
        LOGGER.debug("Codex %s output: %s", stream_label, text)
