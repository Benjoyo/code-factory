from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from ..config import validate_dispatch_settings
from ..observability import ObservabilityHTTPServer
from ..runtime.orchestration import OrchestratorActor
from ..workflow import WorkflowStoreActor
from .dashboard import (
    LiveStatusDashboard,
    StatusDashboardContext,
    dashboard_url,
    project_url,
)
from .logging import configure_logging

LOGGER = logging.getLogger(__name__)


class SymphonyService:
    def __init__(
        self,
        workflow_path: str,
        *,
        logs_root: str | None = None,
        port_override: int | None = None,
    ) -> None:
        self.workflow_path = workflow_path
        self.logs_root = logs_root
        self.port_override = port_override
        self.orchestrator: OrchestratorActor | None = None
        self.workflow_store: WorkflowStoreActor | None = None

    async def run_forever(self) -> None:
        stop_event = asyncio.Event()
        self._install_signal_handlers(stop_event)
        initial_snapshot = await WorkflowStoreActor(
            self.workflow_path, on_snapshot=self._ignore_snapshot
        ).load_initial_snapshot()
        validate_dispatch_settings(initial_snapshot.settings)
        dashboard_enabled = LiveStatusDashboard.enabled(
            initial_snapshot.settings, sys.stderr
        )
        log_path = self._configure_logging(dashboard_enabled)
        self._log_startup(initial_snapshot, log_path=log_path)

        orchestrator = OrchestratorActor(initial_snapshot)
        workflow_store = WorkflowStoreActor(
            self.workflow_path,
            on_snapshot=orchestrator.notify_workflow_updated,
            on_error=orchestrator.notify_workflow_reload_error,
        )
        self.orchestrator = orchestrator
        self.workflow_store = workflow_store
        await orchestrator.startup_terminal_workspace_cleanup()

        http_server = self._build_http_server(initial_snapshot, orchestrator)
        status_dashboard = self._build_status_dashboard(initial_snapshot, orchestrator)
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(orchestrator.run(stop_event))
            task_group.create_task(workflow_store.run(stop_event))
            if http_server is not None:
                task_group.create_task(http_server.run(stop_event))
            if status_dashboard is not None:
                task_group.create_task(status_dashboard.run(stop_event))
            if status_dashboard is not None and self._dashboard_input_supported():
                task_group.create_task(self._monitor_dashboard_input(stop_event))
            await stop_event.wait()
            await orchestrator.shutdown()

    async def _ignore_snapshot(self, _snapshot) -> None:
        return None

    def _build_http_server(
        self, initial_snapshot, orchestrator: OrchestratorActor
    ) -> ObservabilityHTTPServer | None:
        effective_port = (
            self.port_override
            if self.port_override is not None
            else initial_snapshot.settings.server.port
        )
        if not isinstance(effective_port, int):
            LOGGER.info(
                "Observability API disabled; set `server.port` in WORKFLOW.md or pass `--port` to enable it"
            )
            return None
        LOGGER.info(
            "Observability API enabled host=%s port=%s",
            initial_snapshot.settings.server.host,
            effective_port,
        )
        return ObservabilityHTTPServer(
            orchestrator,
            host=initial_snapshot.settings.server.host,
            port=effective_port,
        )

    def _build_status_dashboard(
        self, initial_snapshot, orchestrator: OrchestratorActor
    ) -> LiveStatusDashboard | None:
        if not LiveStatusDashboard.enabled(initial_snapshot.settings, sys.stderr):
            return None
        return LiveStatusDashboard(
            orchestrator,
            settings=initial_snapshot.settings,
            context=StatusDashboardContext(
                max_agents=initial_snapshot.settings.agent.max_concurrent_agents,
                project_url=project_url(initial_snapshot.settings.tracker.project_slug),
                dashboard_url=dashboard_url(
                    initial_snapshot.settings.server.host,
                    self._effective_port(initial_snapshot),
                ),
            ),
        )

    def _log_startup(self, snapshot, *, log_path: Path | None) -> None:
        LOGGER.info(
            "Symphony starting workflow=%s tracker=%s project=%s polling_interval_ms=%s max_concurrent_agents=%s workspace_root=%s",
            snapshot.path,
            snapshot.settings.tracker.kind,
            snapshot.settings.tracker.project_slug,
            snapshot.settings.polling.interval_ms,
            snapshot.settings.agent.max_concurrent_agents,
            snapshot.settings.workspace.root,
        )
        if log_path is not None:
            LOGGER.info("Rotating log file enabled path=%s", log_path)

    def _configure_logging(self, dashboard_enabled: bool) -> Path | None:
        try:
            return configure_logging(self.logs_root, console=not dashboard_enabled)
        except TypeError as exc:
            if "unexpected keyword argument 'console'" not in str(exc):
                raise
            return configure_logging(self.logs_root)

    def _effective_port(self, initial_snapshot) -> int | None:
        return (
            self.port_override
            if self.port_override is not None
            else initial_snapshot.settings.server.port
        )

    def _dashboard_input_supported(self) -> bool:
        is_tty = getattr(sys.stdin, "isatty", None)
        return bool(callable(is_tty) and is_tty())

    async def _monitor_dashboard_input(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        try:
            stdin_fd = sys.stdin.fileno()
        except (AttributeError, OSError, ValueError):
            return

        def on_input_ready() -> None:
            line = sys.stdin.readline()
            if not line:
                stop_event.set()
                return
            if line.strip().lower() in {"q", "quit", "exit"}:
                stop_event.set()

        try:
            loop.add_reader(stdin_fd, on_input_ready)
        except NotImplementedError:
            return
        try:
            await stop_event.wait()
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(stdin_fd)

    def _install_signal_handlers(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
