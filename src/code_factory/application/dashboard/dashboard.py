"""Dashboard orchestration that keeps a live Rich view of the orchestrator snapshot."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console, RenderableType
from rich.live import Live

from ...config.models import Settings
from ...runtime.orchestration import OrchestratorActor
from ...runtime.support import monotonic_ms
from ...workflow.models import WorkflowSnapshot
from .dashboard_diagnostics import DashboardDiagnostics
from .dashboard_render import StatusDashboardContext, render_status_dashboard
from .dashboard_workflow import dashboard_url, project_url

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
        diagnostics: DashboardDiagnostics | None = None,
        console: Console | None = None,
        stream: object | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._context = context
        self._diagnostics = diagnostics
        self._console = console or Console(stderr=True)
        self._stream = stream or self._console.file
        self._sleep_ms = max(250, min(settings.observability.refresh_ms, 1_000))
        self._samples: list[tuple[int, int]] = []
        self._last_snapshot_error: str | None = None
        self._enabled = bool(getattr(settings.observability, "dashboard_enabled", True))
        self._config_event = asyncio.Event()

    @staticmethod
    def stream_supported(stream: object) -> bool:
        """Positive when the stream appears attached to a tty."""

        is_tty = getattr(stream, "isatty", None)
        return bool(callable(is_tty) and is_tty())

    @staticmethod
    def enabled(settings: Settings, stream: object) -> bool:
        """Positive when the stream appears attached to a tty and dashboards are enabled."""

        enabled = getattr(settings.observability, "dashboard_enabled", True)
        return enabled and LiveStatusDashboard.stream_supported(stream)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Refresh the live renderable at `refresh_ms` until the stop event fires."""

        while not stop_event.is_set():
            if not self._enabled:
                if await self._wait_for_stop_or_config(stop_event, timeout=None):
                    return
                continue
            with Live(
                self._render_unavailable(),
                auto_refresh=False,
                console=self._console,
                screen=False,
                vertical_overflow="ellipsis",
            ) as live:
                while self._enabled and not stop_event.is_set():
                    live.update(await self._snapshot_renderable(), refresh=True)
                    if await self._wait_for_stop_or_config(
                        stop_event, timeout=self._sleep_ms / 1000
                    ):
                        return

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
        self._sleep_ms = _dashboard_refresh_ms(snapshot, self._sleep_ms)
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
            recent_logs=self._recent_logs(),
            unavailable=False,
        )

    def _render_unavailable(self) -> RenderableType:
        """Render the dashboard in its offline state while showing the last error."""

        return render_status_dashboard(
            {},
            self._context,
            throughput_tps=0.0,
            recent_logs=self._recent_logs(),
            unavailable=True,
            unavailable_detail=self._last_snapshot_error,
        )

    def _recent_logs(self):
        return self._diagnostics.entries() if self._diagnostics is not None else ()

    async def apply_workflow_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        self._enabled = bool(
            getattr(snapshot.settings.observability, "dashboard_enabled", True)
        )
        self._sleep_ms = max(
            250, min(snapshot.settings.observability.refresh_ms, 1_000)
        )
        self._config_event.set()

    async def apply_workflow_reload_error(self, _error: Any) -> None:
        self._config_event.set()

    async def _wait_for_stop_or_config(
        self, stop_event: asyncio.Event, *, timeout: float | None
    ) -> bool:
        if stop_event.is_set():
            return True
        if timeout is not None:
            try:
                await asyncio.wait_for(self._config_event.wait(), timeout=timeout)
            except TimeoutError:
                return stop_event.is_set()
            self._config_event.clear()
            return stop_event.is_set()
        stop_waiter = asyncio.create_task(stop_event.wait())
        config_waiter = asyncio.create_task(self._config_event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_waiter, config_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (stop_waiter, config_waiter):
                if not task.done():
                    task.cancel()
        if config_waiter in done:
            self._config_event.clear()
        return stop_waiter in done or stop_event.is_set()


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


def _dashboard_refresh_ms(snapshot: dict[str, object], current_ms: int) -> int:
    workflow = snapshot.get("workflow")
    if not isinstance(workflow, dict):
        return current_ms
    observability = workflow.get("observability")
    if not isinstance(observability, dict):
        return current_ms
    value = observability.get("refresh_ms")
    if isinstance(value, int):
        return max(250, min(value, 1_000))
    return current_ms
