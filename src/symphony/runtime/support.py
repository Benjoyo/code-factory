from __future__ import annotations

import asyncio
from typing import Any


async def maybe_aclose(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        maybe_coro = close()
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro


def monotonic_ms() -> int:
    return int(asyncio.get_running_loop().time() * 1000)
