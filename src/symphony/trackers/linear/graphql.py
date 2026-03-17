from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ...config.models import Settings
from ...errors import TrackerClientError

LOGGER = logging.getLogger(__name__)

RequestFunction = Callable[
    [dict[str, Any], list[tuple[str, str]]], Awaitable[httpx.Response]
]


class LinearGraphQLClient:
    MAX_ERROR_BODY_LOG_BYTES = 1000

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        request_fun: RequestFunction | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._request_fun = request_fun

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "variables": variables or {}}
        if operation_name and operation_name.strip():
            payload["operationName"] = operation_name.strip()

        headers = [
            ("Authorization", self._settings.tracker.api_key or ""),
            ("Content-Type", "application/json"),
        ]
        try:
            response = await self._post(payload, headers)
        except httpx.HTTPError as exc:
            LOGGER.error("Linear GraphQL request failed: %r", exc)
            raise TrackerClientError(("linear_api_request", repr(exc))) from exc

        if response.status_code != 200:
            LOGGER.error(
                "Linear GraphQL request failed status=%s body=%s",
                response.status_code,
                summarize_error_body(response),
            )
            raise TrackerClientError(("linear_api_status", response.status_code))

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise TrackerClientError("linear_unknown_payload") from exc

    async def _post(
        self,
        payload: dict[str, Any],
        headers: list[tuple[str, str]],
    ) -> httpx.Response:
        if self._request_fun is not None:
            return await self._request_fun(payload, headers)
        return await self._client.post(
            self._settings.tracker.endpoint,
            headers=dict(headers),
            json=payload,
        )


def summarize_error_body(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        body = response.text
    serialized = body if isinstance(body, str) else repr(body)
    serialized = " ".join(serialized.split())
    if len(serialized) > LinearGraphQLClient.MAX_ERROR_BODY_LOG_BYTES:
        serialized = (
            serialized[: LinearGraphQLClient.MAX_ERROR_BODY_LOG_BYTES]
            + "...<truncated>"
        )
    return serialized
