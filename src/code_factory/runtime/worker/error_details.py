"""Helpers for surfacing compact worker failure details in logs."""

from __future__ import annotations

from ...errors import AppServerError


def format_worker_failure(exc: BaseException) -> str:
    """Summarize the exception chain for worker failure logs."""

    chain: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(_describe_exception(current))
        current = current.__cause__ or current.__context__
    return " <- ".join(chain)


def _describe_exception(exc: BaseException) -> str:
    if isinstance(exc, AppServerError):
        return f"{type(exc).__name__}(reason={exc.reason!r})"
    message = str(exc)
    if message:
        return f"{type(exc).__name__}({message})"
    return repr(exc)
