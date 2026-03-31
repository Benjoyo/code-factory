"""Observer hooks for the operator review flow."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .review_models import RunningReviewServer


class ReviewObserver(Protocol):
    """Render or record operator-visible review updates."""

    def on_prepare_line(self, label: str, stream_name: str, line: str) -> None: ...

    def on_server_started(self, entry: RunningReviewServer) -> None: ...

    def on_servers_ready(self, running: Sequence[RunningReviewServer]) -> None: ...

    def on_server_line(
        self, entry: RunningReviewServer, stream_name: str, line: str
    ) -> None: ...

    def on_warning(self, message: str) -> None: ...


class NullReviewObserver:
    """Default no-op observer used when no rendering is needed."""

    def on_prepare_line(self, label: str, stream_name: str, line: str) -> None:
        return

    def on_server_started(self, entry: RunningReviewServer) -> None:
        return

    def on_servers_ready(self, running: Sequence[RunningReviewServer]) -> None:
        return

    def on_server_line(
        self, entry: RunningReviewServer, stream_name: str, line: str
    ) -> None:
        return

    def on_warning(self, message: str) -> None:
        return
