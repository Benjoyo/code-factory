"""HTTP readiness helpers for review browser launching."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

_READINESS_TIMEOUT_S = 30.0
_READINESS_INTERVAL_S = 0.25
_READINESS_REQUEST_TIMEOUT_S = 1.0


async def wait_for_http_ready(
    url: str,
    *,
    timeout_s: float = _READINESS_TIMEOUT_S,
    interval_s: float = _READINESS_INTERVAL_S,
    client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> bool:
    """Return True once the URL responds to HTTP, regardless of status code."""

    deadline = time.monotonic() + timeout_s
    async with client_factory(
        timeout=httpx.Timeout(_READINESS_REQUEST_TIMEOUT_S),
        follow_redirects=True,
    ) as client:
        while True:
            try:
                await client.get(url)
                return True
            except httpx.RequestError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(interval_s, remaining))
