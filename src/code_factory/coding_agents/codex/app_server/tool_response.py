"""Helpers that translate transport-neutral tool results into app-server payloads."""

from __future__ import annotations

import json
from typing import Any

from ..tools.results import ToolExecutionOutcome


def encode_payload(payload: Any) -> str:
    """Serialize result payloads in a predictable format for Codex consumption."""

    return (
        json.dumps(payload, indent=2, sort_keys=True)
        if isinstance(payload, dict | list)
        else repr(payload)
    )


def build_tool_response(result: ToolExecutionOutcome) -> dict[str, Any]:
    """Wrap a transport-neutral tool outcome in the app-server result envelope."""

    return {
        "success": result.success,
        "contentItems": [{"type": "inputText", "text": encode_payload(result.payload)}],
    }
