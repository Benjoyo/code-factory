"""Entrypoint that wires together the runtime pieces of the long-lived service."""

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
)
from .dashboard.dashboard_diagnostics import DashboardDiagnostics
from .dashboard.dashboard_workflow import dashboard_url
from .log_paths import resolve_logs_root
from .logging import configure_logging
from .project_links import resolve_project_url

LOGGER = logging.getLogger(__name__)


class CodeFactoryService:
    """Facilitates startup, graceful shutdown, and dashboard plumbing for the service."""

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
        """Boots the orchestrator, workflow store, dashboard, and optional HTTP server."""

        stop_event = asyncio.Event()
        self._install_signal_handlers(stop_event)
        workflow_store = WorkflowStoreActor(
            self.workflow_path, on_snapshot=self._ignore_snapshot
        )
        initial_snapshot = await workflow_store.load_initial_snapshot()
        validate_dispatch_settings(initial_snapshot.settings)
        dashboard_supported = LiveStatusDashboard.stream_supported(sys.stderr)
        diagnostics = DashboardDiagnostics() if dashboard_supported else None
        log_path = self._configure_logging(
            dashboard_supported,
            diagnostics,
            settings=initial_snapshot.settings,
        )
        self._log_startup(initial_snapshot, log_path=log_path)

        reload_workflow_if_changed = getattr(workflow_store, "reload_if_changed", None)
        try:
            orchestrator = OrchestratorActor(
                initial_snapshot,
                reload_workflow_if_changed=reload_workflow_if_changed,
            )
        except TypeError as exc:
            if "unexpected keyword argument 'reload_workflow_if_changed'" not in str(
                exc
            ):
                raise
            orchestrator = OrchestratorActor(initial_snapshot)
        self._subscribe_workflow_runtime(
            workflow_store,
            on_snapshot=getattr(orchestrator, "notify_workflow_updated", None),
            on_error=getattr(orchestrator, "notify_workflow_reload_error", None),
        )
        self.orchestrator = orchestrator
        self.workflow_store = workflow_store
        await orchestrator.startup_terminal_workspace_cleanup()

        http_server = self._build_http_server(initial_snapshot, orchestrator)
        if http_server is not None:
            self._subscribe_workflow_runtime(
                workflow_store,
                on_snapshot=getattr(http_server, "apply_workflow_snapshot", None),
                on_error=getattr(http_server, "apply_workflow_reload_error", None),
            )
        dashboard_requested = LiveStatusDashboard.stream_supported(
            sys.stderr
        ) or LiveStatusDashboard.enabled(initial_snapshot.settings, sys.stderr)
        status_dashboard = self._build_status_dashboard(
            initial_snapshot,
            orchestrator,
            diagnostics,
            await resolve_project_url(initial_snapshot.settings)
            if dashboard_requested
            else None,
        )
        if status_dashboard is not None:
            self._subscribe_workflow_runtime(
                workflow_store,
                on_snapshot=getattr(status_dashboard, "apply_workflow_snapshot", None),
                on_error=getattr(status_dashboard, "apply_workflow_reload_error", None),
            )
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
        """Placeholder callback used before the orchestrator is wired into the workflow store."""
        return None

    def _subscribe_workflow_runtime(
        self,
        workflow_store,
        *,
        on_snapshot,
        on_error,
    ) -> None:
        subscribe = getattr(workflow_store, "subscribe", None)
        if callable(subscribe) and (callable(on_snapshot) or callable(on_error)):
            subscribe(on_snapshot=on_snapshot, on_error=on_error)

    def _build_http_server(
        self, initial_snapshot, orchestrator: OrchestratorActor
    ) -> ObservabilityHTTPServer | None:
        """Create the observability API manager with the current effective endpoint."""

        effective_port = (
            self.port_override
            if self.port_override is not None
            else initial_snapshot.settings.server.port
        )
        if not isinstance(effective_port, int):
            LOGGER.info(
                "Observability API disabled; this should no longer happen with default server settings"
            )
        else:
            LOGGER.info(
                "Observability API enabled host=%s port=%s",
                initial_snapshot.settings.server.host,
                effective_port,
            )
        try:
            return ObservabilityHTTPServer(
                orchestrator,
                host=initial_snapshot.settings.server.host,
                port=initial_snapshot.settings.server.port,
                port_override=self.port_override,
                workflow_path=self.workflow_path,
                fail_fast_on_startup=True,
            )
        except TypeError as exc:
            if "unexpected keyword argument 'port_override'" not in str(exc):
                raise
            return ObservabilityHTTPServer(
                orchestrator,
                host=initial_snapshot.settings.server.host,
                port=effective_port,
            )

    def _build_status_dashboard(
        self,
        initial_snapshot,
        orchestrator: OrchestratorActor,
        diagnostics: DashboardDiagnostics | None = None,
        project_url_override: str | None = None,
    ) -> LiveStatusDashboard | None:
        """Create the live TUI dashboard if the workflow declares it."""

        if not (
            LiveStatusDashboard.stream_supported(sys.stderr)
            or LiveStatusDashboard.enabled(initial_snapshot.settings, sys.stderr)
        ):
            return None
        return LiveStatusDashboard(
            orchestrator,
            settings=initial_snapshot.settings,
            diagnostics=diagnostics,
            context=StatusDashboardContext(
                max_agents=initial_snapshot.settings.agent.max_concurrent_agents,
                project_url=project_url_override,
                dashboard_url=dashboard_url(
                    initial_snapshot.settings.server.host,
                    self._effective_port(initial_snapshot),
                ),
                port_override=self.port_override,
            ),
            stream=sys.stderr,
        )

    def _log_startup(self, snapshot, *, log_path: Path | None) -> None:
        """Emit startup metadata so operators know which workflow and tracker are active."""

        LOGGER.info(
            "Code Factory starting workflow=%s tracker=%s project=%s polling_interval_ms=%s max_concurrent_agents=%s workspace_root=%s",
            snapshot.path,
            snapshot.settings.tracker.kind,
            snapshot.settings.tracker.project,
            snapshot.settings.polling.interval_ms,
            snapshot.settings.agent.max_concurrent_agents,
            snapshot.settings.workspace.root,
        )
        if log_path is not None:
            LOGGER.info("Rotating log file enabled path=%s", log_path)

    def _configure_logging(
        self,
        dashboard_enabled: bool,
        diagnostics: DashboardDiagnostics | None = None,
        *,
        settings=None,
    ) -> Path | None:
        """Call the shared logging helper, falling back for dashboards that rewire handlers."""

        logs_root = self._resolved_logs_root(settings)
        try:
            return configure_logging(
                logs_root,
                console=not dashboard_enabled,
                diagnostics=diagnostics,
            )
        except TypeError as exc:
            if "unexpected keyword argument 'diagnostics'" in str(exc):
                try:
                    return configure_logging(logs_root, console=not dashboard_enabled)
                except TypeError as inner:
                    if "unexpected keyword argument 'console'" not in str(inner):
                        raise
                    return configure_logging(logs_root)
            if "unexpected keyword argument 'console'" not in str(exc):
                raise
            return configure_logging(logs_root)

    def _resolved_logs_root(self, settings) -> str | None:
        """Resolve the effective file-log root from CLI override and workflow config."""

        observability = getattr(settings, "observability", None)
        file_logging = getattr(observability, "file_logging", None)
        return resolve_logs_root(
            self.workflow_path,
            override=self.logs_root,
            file_logging_enabled=getattr(file_logging, "enabled", True),
            configured_root=getattr(file_logging, "root", None),
        )

    def _effective_port(self, initial_snapshot) -> int | None:
        """Compute the port used by dashboards or API, preferring the CLI override."""

        return (
            self.port_override
            if self.port_override is not None
            else initial_snapshot.settings.server.port
        )

    def _dashboard_input_supported(self) -> bool:
        """Return True when stdin behaves like a tty so we can read dashboard commands."""

        is_tty = getattr(sys.stdin, "isatty", None)
        return bool(callable(is_tty) and is_tty())

    async def _monitor_dashboard_input(self, stop_event: asyncio.Event) -> None:
        """Watch stdin so typing 'quit' also triggers service shutdown for the dashboard."""

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
        """Add SIGTERM handling when the event loop supports it."""

        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
