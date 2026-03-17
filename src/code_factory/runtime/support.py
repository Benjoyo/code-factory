"""Minimal runtime helpers shared across runtime components."""

from __future__ import annotations

import asyncio
from typing import Any


async def maybe_aclose(resource: Any) -> None:
    """Gracefully close a resource if it exposes a close method, awaiting it when needed."""
    close = getattr(resource, "close", None)
    if callable(close):
        maybe_coro = close()
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro


def monotonic_ms() -> int:
    """Return the current loop time in milliseconds for scheduling/logging deadlines."""
    return int(asyncio.get_running_loop().time() * 1000)
