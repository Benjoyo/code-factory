"""Shared project-name validation and lookup helpers for Linear."""

from __future__ import annotations

import re

from ...errors import ConfigValidationError, TrackerClientError

PROJECT_NAME_EXAMPLE = "test-project"
PROJECT_NAME_GUIDANCE = (
    f'Use the Linear project name (for example: "{PROJECT_NAME_EXAMPLE}"), '
    "not the project URL or slug."
)
_PROJECT_URL_PREFIXES = ("http://", "https://")
_PROJECT_SLUG_ONLY = re.compile(r"^[0-9a-f]{10,}$", re.IGNORECASE)
_PROJECT_URL_SEGMENT = re.compile(r"^.+-[0-9a-f]{10,}$", re.IGNORECASE)

PROJECT_LOOKUP_QUERY = """
query CodeFactoryTrackerProjectByName($name: String!, $first: Int!) {
  projects(filter: { name: { eq: $name } }, first: $first) {
    nodes {
      id
      name
      slugId
      url
      teams(first: 20) {
        nodes {
          id
          name
          key
        }
      }
    }
  }
}
"""


def validate_config_project(config: dict) -> None:
    tracker = config.get("tracker")
    if not isinstance(tracker, dict):
        return
    if "project_slug" in tracker:
        raise ConfigValidationError(
            "`tracker.project_slug` was removed. Use `tracker.project` and set it to the Linear project name.",
            code="legacy_linear_project_field",
        )


def validate_project_name(value: str, *, config_error: bool) -> str:
    normalized = value.strip()
    if not normalized:
        return normalized
    if _looks_like_project_url_or_slug(normalized):
        if config_error:
            raise ConfigValidationError(
                f"tracker.project must be the Linear project name. {PROJECT_NAME_GUIDANCE}",
                code="invalid_linear_project_reference",
            )
        raise TrackerClientError(("tracker_invalid_project_reference", normalized))
    return normalized


def project_not_found_error(name: str) -> TrackerClientError:
    return TrackerClientError(("tracker_project_not_found", name))


def project_ambiguous_error(name: str) -> TrackerClientError:
    return TrackerClientError(("tracker_project_ambiguous", name))


def _looks_like_project_url_or_slug(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered.startswith(_PROJECT_URL_PREFIXES):
        return True
    if "linear.app/" in lowered:
        return True
    if _PROJECT_SLUG_ONLY.fullmatch(lowered):
        return True
    return _PROJECT_URL_SEGMENT.fullmatch(lowered) is not None
