from __future__ import annotations

"""Small HTTP client helpers for talking to the local control-plane API."""

from dataclasses import dataclass
from typing import Any

import httpx

from ...errors import ControlRequestError


@dataclass(frozen=True, slots=True)
class ControlEndpoint:
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def steer_issue(
    endpoint: ControlEndpoint, issue_identifier: str, message: str
) -> dict[str, Any]:
    """Send a steering message to the running local service."""

    try:
        response = httpx.post(
            f"{endpoint.base_url}/api/v1/{issue_identifier}/steer",
            json={"message": message},
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        raise ControlRequestError(
            "control_plane_unreachable",
            f"Could not reach Code Factory at {endpoint.base_url}: {exc}",
            503,
        ) from exc
    payload = response.json() if response.content else {}
    if response.status_code == 202 and isinstance(payload, dict):
        return payload
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else "steer_failed"
    message_text: str
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        message_text = error["message"]
    else:
        message_text = f"Unexpected control-plane response: HTTP {response.status_code}"
    raise ControlRequestError(
        code if isinstance(code, str) else "steer_failed",
        message_text,
        response.status_code,
    )
