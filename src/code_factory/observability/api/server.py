from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from ...runtime.orchestration import OrchestratorActor
from ...workflow.models import WorkflowSnapshot
from .payloads import issue_payload, state_payload

LOGGER = logging.getLogger(__name__)


class ObservabilityHTTPServer:
    def __init__(
        self,
        orchestrator: OrchestratorActor,
        *,
        host: str,
        port: int | None,
        port_override: int | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._host = host
        self._port = port
        self._port_override = port_override
        self._config_event = asyncio.Event()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            runner: web.AppRunner | None = None
            desired = self._desired_endpoint()
            if desired is None:
                if await self._wait_for_stop_or_config(stop_event, timeout=None):
                    return
                continue
            try:
                runner = await self._start_runner()
                if await self._wait_for_stop_or_config(stop_event, timeout=None):
                    return
            except OSError as exc:
                LOGGER.warning("Observability HTTP server failed: %r; retrying", exc)
                if await self._wait_for_stop_or_config(stop_event, timeout=5):
                    return
            finally:
                if runner is not None:
                    await runner.cleanup()

    async def _start_runner(
        self, host: str | None = None, port: int | None = None
    ) -> web.AppRunner:
        app = web.Application()
        app.router.add_get("/api/v1/state", self.state)
        app.router.add_post("/api/v1/refresh", self.refresh)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            app.router.add_route(method, "/api/v1/state", self.method_not_allowed)
        for method in ("GET", "PUT", "PATCH", "DELETE"):
            app.router.add_route(method, "/api/v1/refresh", self.method_not_allowed)
        app.router.add_get("/api/v1/{issue_identifier}", self.issue)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            app.router.add_route(
                method, "/api/v1/{issue_identifier}", self.method_not_allowed
            )
        app.router.add_route("*", "/{tail:.*}", self.not_found)
        runner = web.AppRunner(app)
        await runner.setup()
        resolved_host = host or self._host
        resolved_port = self._effective_port() if port is None else port
        assert isinstance(resolved_port, int)
        site = web.TCPSite(runner, host=resolved_host, port=resolved_port)
        await site.start()
        bound_port = site_bound_port(site) or resolved_port
        LOGGER.info(
            "Observability API listening on http://%s:%s/", resolved_host, bound_port
        )
        return runner

    async def state(self, _request: web.Request) -> web.Response:
        return web.json_response(state_payload(await self._orchestrator.snapshot()))

    async def issue(self, request: web.Request) -> web.Response:
        payload = issue_payload(
            request.match_info["issue_identifier"], await self._orchestrator.snapshot()
        )
        if payload is None:
            return web.json_response(
                {"error": {"code": "issue_not_found", "message": "Issue not found"}},
                status=404,
            )
        return web.json_response(payload)

    async def refresh(self, _request: web.Request) -> web.Response:
        return web.json_response(await self._orchestrator.request_refresh(), status=202)

    async def not_found(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"error": {"code": "not_found", "message": "Route not found"}}, status=404
        )

    async def method_not_allowed(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "error": {
                    "code": "method_not_allowed",
                    "message": "Method not allowed",
                }
            },
            status=405,
        )

    async def apply_workflow_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        previous = self._desired_endpoint()
        settings = snapshot.settings.server
        self._host = settings.host
        self._port = settings.port
        if self._desired_endpoint() != previous:
            self._config_event.set()

    async def apply_workflow_reload_error(self, _error: Any) -> None:
        return None

    def _effective_port(self) -> int | None:
        return self._port_override if self._port_override is not None else self._port

    def _desired_endpoint(self) -> tuple[str, int] | None:
        effective_port = self._effective_port()
        if not isinstance(effective_port, int):
            return None
        return self._host, effective_port

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


def site_bound_port(site: web.TCPSite) -> int | None:
    server = getattr(site, "_server", None)
    sockets = getattr(server, "sockets", None)
    if not isinstance(sockets, tuple | list) or not sockets:
        return None
    sockname = sockets[0].getsockname()
    return (
        sockname[1]
        if isinstance(sockname, tuple)
        and len(sockname) >= 2
        and isinstance(sockname[1], int)
        else None
    )
