"""Shared error payload helpers for tracker operations backed by Linear."""

from __future__ import annotations

from typing import Any

from ....errors import TrackerClientError


def linear_error_payload(reason: Exception) -> dict[str, Any]:
    """Convert tracker/client failures into user-facing tracker tool payloads."""

    normalized = (
        reason.reason if isinstance(reason, TrackerClientError) else str(reason)
    )
    if normalized == "missing_linear_api_token":
        return {
            "error": {
                "message": "Code Factory is missing Linear auth. Set `linear.api_key` in `WORKFLOW.md` or export `LINEAR_API_KEY`."
            }
        }
    if normalized == "missing_linear_project_slug":
        return {
            "error": {
                "message": "Code Factory is missing the default tracker project. Set `tracker.project_slug` in `WORKFLOW.md` or pass an explicit project."
            }
        }
    if (
        isinstance(normalized, tuple)
        and len(normalized) == 2
        and normalized[0] == "linear_api_status"
    ):
        return {
            "error": {
                "message": f"Tracker request failed with HTTP {normalized[1]}.",
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
                "message": "Tracker request failed before receiving a successful response.",
                "reason": repr(normalized[1]),
            }
        }
    if (
        isinstance(normalized, tuple)
        and len(normalized) == 2
        and normalized[0]
        in {
            "tracker_file_error",
            "tracker_operation_failed",
            "tracker_missing_field",
            "tracker_ambiguous",
        }
    ):
        return {"error": {"message": str(normalized[1])}}
    if (
        isinstance(normalized, tuple)
        and len(normalized) == 2
        and normalized[0] == "tracker_not_found"
    ):
        return {"error": {"message": f"Tracker record not found: {normalized[1]}"}}
    return {
        "error": {
            "message": "Tracker operation failed.",
            "reason": repr(normalized),
        }
    }
