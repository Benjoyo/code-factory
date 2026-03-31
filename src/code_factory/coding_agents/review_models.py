from __future__ import annotations

"""Structured AI review payloads shared by runtimes and worker gates."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ReviewLineRange:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ReviewCodeLocation:
    absolute_file_path: str
    line_range: ReviewLineRange


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    title: str
    body: str
    code_location: ReviewCodeLocation
    confidence_score: float
    priority: int | None


@dataclass(frozen=True, slots=True)
class ReviewOutput:
    findings: tuple[ReviewFinding, ...]
    overall_correctness: str
    overall_explanation: str
    overall_confidence_score: float


def normalize_review_output(value: Any) -> ReviewOutput | None:
    """Return a normalized AI review payload when the value matches shape."""

    if not isinstance(value, dict):
        return None
    findings = _normalize_review_findings(value.get("findings"))
    overall_correctness = _non_blank_string(value.get("overall_correctness"))
    overall_explanation = _non_blank_string(value.get("overall_explanation"))
    overall_confidence_score = _float_like(value.get("overall_confidence_score"))
    if (
        findings is None
        or overall_correctness is None
        or overall_explanation is None
        or overall_confidence_score is None
    ):
        return None
    return ReviewOutput(
        findings=findings,
        overall_correctness=overall_correctness,
        overall_explanation=overall_explanation,
        overall_confidence_score=overall_confidence_score,
    )


def _normalize_review_findings(value: Any) -> tuple[ReviewFinding, ...] | None:
    if not isinstance(value, list):
        return None
    normalized: list[ReviewFinding] = []
    for entry in value:
        finding = _normalize_review_finding(entry)
        if finding is None:
            return None
        normalized.append(finding)
    return tuple(normalized)


def _normalize_review_finding(value: Any) -> ReviewFinding | None:
    if not isinstance(value, dict):
        return None
    title = _non_blank_string(value.get("title"))
    body = _non_blank_string(value.get("body"))
    location = _normalize_code_location(value.get("code_location"))
    confidence_score = _float_like(value.get("confidence_score"))
    priority = _nullable_int_like(value.get("priority"))
    if title is None or body is None or location is None or confidence_score is None:
        return None
    return ReviewFinding(
        title=title,
        body=body,
        code_location=location,
        confidence_score=confidence_score,
        priority=priority,
    )


def _normalize_code_location(value: Any) -> ReviewCodeLocation | None:
    if not isinstance(value, dict):
        return None
    absolute_file_path = _non_blank_string(value.get("absolute_file_path"))
    line_range = _normalize_line_range(value.get("line_range"))
    if absolute_file_path is None or line_range is None:
        return None
    return ReviewCodeLocation(
        absolute_file_path=absolute_file_path,
        line_range=line_range,
    )


def _normalize_line_range(value: Any) -> ReviewLineRange | None:
    if not isinstance(value, dict):
        return None
    start = _int_like(value.get("start"))
    end = _int_like(value.get("end"))
    if start is None or end is None or start < 1 or end < start:
        return None
    return ReviewLineRange(start=start, end=end)


def _non_blank_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int_like(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _nullable_int_like(value: Any) -> int | None:
    if value is None:
        return None
    return _int_like(value)


def _float_like(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None
