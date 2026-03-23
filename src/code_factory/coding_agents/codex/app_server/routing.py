from __future__ import annotations

"""Routes raw App Server stdout lines to pending responses or turn events."""

import asyncio
import json
from typing import Any

from ....errors import AppServerError
from .streams import log_non_json_stream_line


async def route_stdout(
    raw_queue: asyncio.Queue[tuple[str, Any]],
    event_queue: asyncio.Queue[tuple[str, Any]],
    pending_requests: dict[int, asyncio.Future[dict[str, Any]]],
) -> None:
    """Continuously fan out responses to waiters and notifications to the turn loop."""

    try:
        while True:
            kind, payload = await raw_queue.get()
            if kind == "exit":
                error = AppServerError(("port_exit", payload))
                for request_id in list(pending_requests):
                    future = pending_requests.pop(request_id)
                    if not future.done():
                        future.set_exception(error)
                await event_queue.put((kind, payload))
                return
            if kind != "line":
                await event_queue.put((kind, payload))
                continue
            try:
                message = json.loads(payload)
            except json.JSONDecodeError:
                log_non_json_stream_line(payload, "response stream")
                await event_queue.put((kind, payload))
                continue
            request_id = message.get("id")
            if isinstance(request_id, int) and request_id in pending_requests:
                future = pending_requests.pop(request_id)
                if not future.done():
                    if "error" in message:
                        future.set_exception(
                            AppServerError(("response_error", message["error"]))
                        )
                    else:
                        result = message.get("result")
                        if isinstance(result, dict):
                            future.set_result(result)
                        else:
                            future.set_exception(
                                AppServerError(("response_error", message))
                            )
                continue
            await event_queue.put((kind, payload))
    except asyncio.CancelledError:
        for request_id in list(pending_requests):
            future = pending_requests.pop(request_id)
            if not future.done():
                future.cancel()
        raise
