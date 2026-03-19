"""Shared error payload helpers for tools backed by Linear GraphQL."""

from __future__ import annotations

from typing import Any

from ....errors import TrackerClientError


def linear_error_payload(reason: Exception) -> dict[str, Any]:
    """Convert tracker/client failures into user-facing Linear tool payloads."""

    normalized = (
        reason.reason if isinstance(reason, TrackerClientError) else str(reason)
    )
    if normalized == "missing_linear_api_token":
        return {
            "error": {
                "message": "Code Factory is missing Linear auth. Set `linear.api_key` in `WORKFLOW.md` or export `LINEAR_API_KEY`."
            }
        }
    if (
        isinstance(normalized, tuple)
        and len(normalized) == 2
        and normalized[0] == "linear_api_status"
    ):
        return {
            "error": {
                "message": f"Linear GraphQL request failed with HTTP {normalized[1]}.",
                "status": normalized[1],
            }
        }
    if (
        isinstance(normalized, tuple)
        and len(normalized) == 2
        and normalized[0] == "linear_api_request"
    ):
        return {
            "error": {
                "message": "Linear GraphQL request failed before receiving a successful response.",
                "reason": repr(normalized[1]),
            }
        }
    return {
        "error": {
            "message": "Linear GraphQL tool execution failed.",
            "reason": repr(normalized),
        }
    }
