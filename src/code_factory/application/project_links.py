"""Application-layer helpers for resolving dashboard-facing project links."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from ..errors import TrackerClientError
from .dashboard.dashboard_workflow import project_url

LOGGER = logging.getLogger(__name__)


async def resolve_project_url(settings: Any) -> str | None:
    """Best-effort lookup for the configured Linear project URL."""

    if (
        settings.tracker.kind != "linear"
        or not settings.tracker.api_key
        or not settings.tracker.project
    ):
        return None
    bootstrap_module = importlib.import_module("code_factory.trackers.linear.bootstrap")
    bootstrapper = bootstrap_module.LinearBootstrapper(
        api_key=settings.tracker.api_key,
        endpoint=settings.tracker.endpoint,
    )
    try:
        project = await bootstrapper.resolve_project(settings.tracker.project)
    except TrackerClientError as exc:
        LOGGER.warning("Unable to resolve Linear project URL: %s", exc)
        return None
    finally:
        await bootstrapper.close()
    return project_url(project.url if project is not None else None)
