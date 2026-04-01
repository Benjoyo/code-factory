"""UI selection and workflow wiring for `cf review`."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console

from ...config import parse_settings
from ...config.models import ReviewSettings
from ...errors import ReviewError
from ...workflow.loader import load_workflow
from .review_models import ReviewTarget
from .review_output import ReviewConsoleObserver
from .review_resolution import resolve_repo_root, resolve_review_target
from .review_runner import ReviewRunner, _review_temp_root


class ReviewUiUnavailableError(RuntimeError):
    """Raised when the interactive review UI can't be started."""


async def run_review_session(
    workflow_path: str,
    target: str,
    *,
    keep: bool,
    console: Console | None = None,
) -> None:
    settings = parse_settings(load_workflow(workflow_path).config)
    if not settings.review.servers:
        raise ReviewError("`review.servers` must be configured in WORKFLOW.md.")
    repo_root = await resolve_repo_root(workflow_path)
    resolved_target = await resolve_review_target(repo_root, settings, target)
    runner = ReviewRunner(
        repo_root=repo_root,
        worktree_root=_review_temp_root(settings.review.temp_root, repo_root),
        keep=keep,
        prepare_command=settings.review.prepare,
    )
    if _interactive_review_supported():
        try:
            await run_review_textual_session(
                runner, repo_root, resolved_target, settings.review
            )
            return
        except ReviewUiUnavailableError:
            pass
    await runner.run(
        resolved_target,
        settings.review.servers,
        observer=ReviewConsoleObserver(console or Console()),
    )


async def run_review_textual_session(
    runner: ReviewRunner,
    repo_root: str,
    target: ReviewTarget,
    review: ReviewSettings,
) -> None:
    try:
        from .review_textual_app import ReviewTextualApp
    except Exception as exc:  # pragma: no cover - import failure depends on env
        raise ReviewUiUnavailableError(str(exc)) from exc
    app = ReviewTextualApp(
        repo_root=repo_root,
        target=target,
        servers=review.servers,
        prepare_enabled=bool(review.prepare),
        run_session=lambda observer, stop_event: runner.run(
            target,
            review.servers,
            observer=observer,
            stop_event=stop_event,
        ),
    )
    try:
        await app.run_async()
    except Exception as exc:  # pragma: no cover - startup failure depends on env
        raise ReviewUiUnavailableError(str(exc)) from exc
    if app.session_error is not None:
        raise app.session_error


def _interactive_review_supported(
    stdin: Any = sys.stdin, stdout: Any = sys.stdout
) -> bool:
    return _isatty(stdin) and _isatty(stdout)


def _isatty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())
