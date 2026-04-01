"""Template and environment helpers for review worktree commands."""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from liquid import Environment, StrictUndefined

from ...config.models import ReviewServerSettings
from ...errors import ReviewError
from ...prompts.values import to_liquid_value
from .review_models import ReviewLaunch, ReviewTarget

LIQUID_ENV = Environment(undefined=StrictUndefined)


def build_review_launch(
    target: ReviewTarget,
    worktree: str,
    server: ReviewServerSettings,
) -> ReviewLaunch:
    port = computed_review_port(server.base_port, target)
    context = review_context(target, worktree, port)
    url = _render_optional(server.url, context, server=server)
    return ReviewLaunch(
        name=server.name,
        command=render_review_template(server.command, context, server=server),
        port=port,
        url=url,
        open_browser=_effective_open_browser(server, url),
    )


def build_review_environment(
    target: ReviewTarget,
    *,
    worktree: str,
    port: int | None,
) -> dict[str, str]:
    environment = dict(os.environ)
    context = review_context(target, worktree, port)["review"]
    mapping = {
        "CF_REVIEW_TARGET": context["target"],
        "CF_REVIEW_KIND": context["kind"],
        "CF_REVIEW_TICKET_IDENTIFIER": context["ticket_identifier"],
        "CF_REVIEW_TICKET_NUMBER": context["ticket_number"],
        "CF_REVIEW_WORKTREE": context["worktree"],
        "CF_REVIEW_REF": context["ref"],
        "CF_REVIEW_PORT": context["port"],
    }
    for key, value in mapping.items():
        if value is None:
            continue
        environment[key] = str(value)
    return environment


def review_context(
    target: ReviewTarget,
    worktree: str,
    port: int | None,
) -> dict[str, dict[str, Any]]:
    return {
        "review": {
            "target": target.target,
            "kind": target.kind,
            "ticket_identifier": target.ticket_identifier,
            "ticket_number": target.ticket_number,
            "worktree": worktree,
            "ref": target.ref,
            "port": port,
        }
    }


def computed_review_port(base_port: int | None, target: ReviewTarget) -> int | None:
    if base_port is None:
        return None
    if target.kind == "main":
        return _validated_port(base_port)
    if target.ticket_number is None:
        raise ReviewError(
            f"{target.target} does not end with digits, so review server ports "
            "cannot be derived from base_port."
        )
    return _validated_port(base_port + target.ticket_number)


def render_review_template(
    template: str,
    context: dict[str, dict[str, Any]],
    *,
    server: ReviewServerSettings | None = None,
) -> str:
    payload: dict[str, Any] = dict(context)
    if server is not None:
        payload["server"] = to_liquid_value(asdict(server))
    try:
        return (
            LIQUID_ENV.from_string(template).render(**to_liquid_value(payload)).strip()
        )
    except Exception as exc:
        name = server.name if server is not None else "review"
        raise ReviewError(f"Failed to render {name} review template: {exc}") from exc


def _render_optional(
    template: str | None,
    context: dict[str, dict[str, Any]],
    *,
    server: ReviewServerSettings,
) -> str | None:
    if template is None:
        return None
    return render_review_template(template, context, server=server)


def _validated_port(port: int) -> int:
    if port < 1 or port > 65_535:
        raise ReviewError(f"Computed review port {port} is outside the valid range.")
    return port


def _effective_open_browser(server: ReviewServerSettings, url: str | None) -> bool:
    if server.open_browser is not None:
        return server.open_browser
    return url is not None
