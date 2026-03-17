from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import parse_settings
from .loader import current_stamp, load_workflow
from .models import WorkflowSnapshot, WorkflowStoreState

LOGGER = logging.getLogger(__name__)


class WorkflowStoreActor:
    def __init__(
        self,
        path: str,
        *,
        on_snapshot: Callable[[WorkflowSnapshot], Awaitable[None]],
        on_error: Callable[[Any], Awaitable[None]] | None = None,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.path = path
        self._on_snapshot = on_snapshot
        self._on_error = on_error
        self._poll_interval_s = poll_interval_s
        self._state: WorkflowStoreState | None = None

    async def load_initial_snapshot(self) -> WorkflowSnapshot:
        definition = load_workflow(self.path)
        settings = parse_settings(definition.config)
        stamp = current_stamp(self.path)
        self._state = WorkflowStoreState(
            path=self.path,
            stamp=stamp,
            workflow=definition,
            version=1,
        )
        return WorkflowSnapshot(
            version=1,
            path=self.path,
            stamp=stamp,
            definition=definition,
            settings=settings,
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        if self._state is None:
            await self.load_initial_snapshot()

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_s)
            except TimeoutError:
                await self._reload_if_changed()

    async def _reload_if_changed(self) -> None:
        state = self._require_state()
        try:
            stamp = current_stamp(self.path)
        except Exception as exc:
            await self._handle_reload_error(exc)
            return

        if stamp == state.stamp:
            return

        try:
            definition = load_workflow(self.path)
            settings = parse_settings(definition.config)
        except Exception as exc:
            await self._handle_reload_error(exc)
            return

        version = state.version + 1
        self._state = WorkflowStoreState(
            path=self.path,
            stamp=stamp,
            workflow=definition,
            version=version,
            last_reload_error=None,
        )
        await self._on_snapshot(
            WorkflowSnapshot(
                version=version,
                path=self.path,
                stamp=stamp,
                definition=definition,
                settings=settings,
            )
        )

    async def _handle_reload_error(self, error: Any) -> None:
        state = self._require_state()
        state.last_reload_error = error
        LOGGER.error(
            "Failed to reload workflow path=%s reason=%r; keeping last known good configuration",
            self.path,
            error,
        )
        if self._on_error is not None:
            await self._on_error(error)

    def _require_state(self) -> WorkflowStoreState:
        assert self._state is not None
        return self._state
