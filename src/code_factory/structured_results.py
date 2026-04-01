"""Shared models and helpers for structured agent turn results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import yaml

RESULT_COMMENT_PREFIX = "## State Result: "
LEGACY_RESULT_COMMENT_PREFIX = "## Code Factory Result: "
RESULT_DECISIONS = ("transition", "blocked")


@dataclass(frozen=True, slots=True)
class StructuredTurnResult:
    """Normalized result emitted by an agent-run workflow state."""

    decision: str
    summary: str
    next_state: str | None = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def structured_turn_output_schema(
    allowed_next_states: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the app-server output schema for workflow state completion."""

    next_state_schema: dict[str, Any]
    if allowed_next_states:
        next_state_schema = {
            "enum": [*allowed_next_states, None],
        }
    else:
        next_state_schema = {
            "type": ["string", "null"],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "summary", "next_state"],
        "properties": {
            "decision": {
                "type": "string",
                "enum": list(RESULT_DECISIONS),
            },
            "summary": {
                "type": "string",
                "minLength": 1,
            },
            "next_state": next_state_schema,
        },
    }


def normalize_structured_turn_result(value: Any) -> StructuredTurnResult | None:
    """Return a normalized structured result when the payload matches the contract."""

    if not isinstance(value, dict):
        return None
    decision = value.get("decision")
    summary = value.get("summary")
    next_state = value.get("next_state")
    if decision not in RESULT_DECISIONS:
        return None
    if not isinstance(summary, str) or not summary.strip():
        return None
    if next_state is None:
        normalized_next_state = None
    elif isinstance(next_state, str) and next_state.strip():
        normalized_next_state = next_state.strip()
    else:
        return None
    return StructuredTurnResult(
        decision=str(decision),
        summary=summary.strip(),
        next_state=normalized_next_state,
    )


def render_result_comment(state_name: str, result: StructuredTurnResult) -> str:
    """Render the persisted result comment for a completed workflow state."""

    next_state = result.next_state or ""
    indented_summary = "\n".join(f"  {line}" for line in result.summary.splitlines())
    return (
        f"{RESULT_COMMENT_PREFIX}{state_name}\n\n"
        f"decision: {result.decision}\n"
        f"next_state: {next_state}\n"
        "summary: |\n"
        f"{indented_summary}\n"
    )


def parse_result_comment(body: str | None) -> tuple[str, StructuredTurnResult] | None:
    """Parse a persisted result comment when it matches the supported format."""

    if not isinstance(body, str):
        return None
    lines = body.splitlines()
    prefix = _result_comment_prefix(lines[0]) if lines else None
    if prefix is None:
        return None
    state_name = lines[0][len(prefix) :].strip()
    if not state_name:
        return None
    document = "\n".join(lines[2:]) if len(lines) > 2 else ""
    parsed = yaml.safe_load(document)
    if not isinstance(parsed, dict):
        return None
    result = normalize_structured_turn_result(parsed)
    if result is None:
        return None
    return state_name, result


def _result_comment_prefix(line: str) -> str | None:
    for prefix in (RESULT_COMMENT_PREFIX, LEGACY_RESULT_COMMENT_PREFIX):
        if line.startswith(prefix):
            return prefix
    return None
