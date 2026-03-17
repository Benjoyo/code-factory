"""Dashboard orchestration that keeps a live Rich view of the orchestrator snapshot."""

from __future__ import annotations

import asyncio

from rich.console import Console, RenderableType
from rich.live import Live

from ..config.models import Settings
from ..runtime.orchestration import OrchestratorActor
from ..runtime.support import monotonic_ms
from .dashboard_render import (
    StatusDashboardContext,
    dashboard_url,
    project_url,
    render_status_dashboard,
)

__all__ = [
    "LiveStatusDashboard",
    "StatusDashboardContext",
    "dashboard_url",
    "project_url",
    "render_status_dashboard",
]


class LiveStatusDashboard:
    """Wraps Rich Live to periodically render the orchestrator snapshot for operators."""

    THROUGHPUT_WINDOW_MS = 5_000

    def __init__(
        self,
        orchestrator: OrchestratorActor,
        *,
        settings: Settings,
        context: StatusDashboardContext,
        console: Console | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._context = context
        self._console = console or Console(stderr=True)
        self._sleep_ms = max(250, min(settings.observability.refresh_ms, 1_000))
        self._samples: list[tuple[int, int]] = []
        self._last_snapshot_error: str | None = None

    @staticmethod
    def enabled(settings: Settings, stream: object) -> bool:
        """Positive when the stream appears attached to a tty and dashboards are enabled."""

        is_tty = getattr(stream, "isatty", None)
        return bool(
            settings.observability.dashboard_enabled and callable(is_tty) and is_tty()
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Refresh the live renderable at `refresh_ms` until the stop event fires."""

        with Live(
            self._render_unavailable(),
            auto_refresh=False,
            console=self._console,
            screen=False,
            vertical_overflow="visible",
        ) as live:
            while True:
                live.update(await self._snapshot_renderable(), refresh=True)
                if stop_event.is_set():
                    return
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self._sleep_ms / 1000
                    )
                except TimeoutError:
                    pass

    async def _snapshot_renderable(self) -> RenderableType:
        """Grab the latest snapshot, then paint the dashboard or a failure notice."""

        try:
            snapshot_now = getattr(self._orchestrator, "snapshot_now", None)
            if callable(snapshot_now):
                raw_snapshot = snapshot_now()
            else:
                raw_snapshot = await asyncio.wait_for(
                    self._orchestrator.snapshot(), timeout=1.0
                )
            if not isinstance(raw_snapshot, dict):
                raise RuntimeError("snapshot payload is not a mapping")
            snapshot = raw_snapshot
        except Exception as exc:
            self._last_snapshot_error = f"{type(exc).__name__}: {exc}"
            return self._render_unavailable()
        now_ms = monotonic_ms()
        self._last_snapshot_error = None
        total_tokens = _total_tokens(snapshot)
        # Keep only the samples in the configured lookback so TPS reflects the last few seconds.
        self._samples = [
            sample
            for sample in [*self._samples, (now_ms, total_tokens)]
            if sample[0] >= now_ms - self.THROUGHPUT_WINDOW_MS
        ]
        return render_status_dashboard(
            snapshot,
            self._context,
            throughput_tps=_rolling_tps(self._samples, now_ms, total_tokens),
            unavailable=False,
        )

    def _render_unavailable(self) -> RenderableType:
        """Render the dashboard in its offline state while showing the last error."""

        return render_status_dashboard(
            {},
            self._context,
            throughput_tps=0.0,
            unavailable=True,
            unavailable_detail=self._last_snapshot_error,
        )


def _total_tokens(snapshot: dict[str, object]) -> int:
    """Safely extract the total tokens field from the dashboard snapshot."""
    totals = snapshot.get("agent_totals")
    if not isinstance(totals, dict):
        return 0
    value = totals.get("total_tokens")
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _rolling_tps(
    samples: list[tuple[int, int]], now_ms: int, current_tokens: int
) -> float:
    """Compute a rolling TPS average over the window while avoiding divide-by-zero."""
    if len(samples) < 2:
        return 0.0
    start_ms, start_tokens = samples[0]
    elapsed_ms = now_ms - start_ms
    if elapsed_ms <= 0:
        return 0.0
    return max(0, current_tokens - start_tokens) / (elapsed_ms / 1000)
