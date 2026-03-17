from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from ...runtime.orchestration import OrchestratorActor
from .payloads import issue_payload, state_payload

LOGGER = logging.getLogger(__name__)


class ObservabilityHTTPServer:
    def __init__(
        self, orchestrator: OrchestratorActor, *, host: str, port: int
    ) -> None:
        self._orchestrator = orchestrator
        self._host = host
        self._port = port

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            runner: web.AppRunner | None = None
            try:
                runner = await self._start_runner()
                await stop_event.wait()
                return
            except OSError as exc:
                LOGGER.warning("Observability HTTP server failed: %r; retrying", exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                except TimeoutError:
                    pass
            finally:
                if runner is not None:
                    await runner.cleanup()

    async def _start_runner(self) -> web.AppRunner:
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
        site = web.TCPSite(runner, host=self._host, port=self._port)
        await site.start()
        bound_port = site_bound_port(site) or self._port
        LOGGER.info(
            "Observability API listening on http://%s:%s/", self._host, bound_port
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
