"""Actor that owns the live `WORKFLOW.md` snapshot for the running service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from watchfiles import awatch

from ..config import parse_settings
from .loader import current_stamp, load_workflow
from .models import WorkflowSnapshot, WorkflowStoreState
from .state_profiles import parse_state_profiles

LOGGER = logging.getLogger(__name__)


class WorkflowStoreActor:
    """Watches, reloads, and publishes the validated workflow snapshot."""

    def __init__(
        self,
        path: str,
        *,
        on_snapshot: Callable[[WorkflowSnapshot], Awaitable[None]] | None = None,
        on_error: Callable[[Any], Awaitable[None]] | None = None,
        poll_interval_s: float = 1.0,
        watch_debounce_ms: int = 150,
    ) -> None:
        self.path = path
        self._snapshot_subscribers: list[
            Callable[[WorkflowSnapshot], Awaitable[None]]
        ] = []
        self._error_subscribers: list[Callable[[Any], Awaitable[None]]] = []
        if on_snapshot is not None:
            self._snapshot_subscribers.append(on_snapshot)
        if on_error is not None:
            self._error_subscribers.append(on_error)
        self._poll_interval_s = poll_interval_s
        self._watch_debounce_ms = watch_debounce_ms
        self._state: WorkflowStoreState | None = None
        self._snapshot: WorkflowSnapshot | None = None

    def subscribe(
        self,
        *,
        on_snapshot: Callable[[WorkflowSnapshot], Awaitable[None]] | None = None,
        on_error: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        """Register additional listeners for validated snapshots or reload errors."""

        if on_snapshot is not None:
            self._snapshot_subscribers.append(on_snapshot)
        if on_error is not None:
            self._error_subscribers.append(on_error)

    async def load_initial_snapshot(self) -> WorkflowSnapshot:
        """Load the first snapshot and initialize internal actor state."""

        definition = load_workflow(self.path)
        settings = parse_settings(definition.config)
        state_profiles = parse_state_profiles(
            definition.config, definition.prompt_sections
        )
        stamp = current_stamp(self.path)
        self._state = WorkflowStoreState(
            path=self.path, stamp=stamp, workflow=definition, version=1
        )
        self._snapshot = WorkflowSnapshot(
            version=1,
            path=self.path,
            stamp=stamp,
            definition=definition,
            settings=settings,
            state_profiles=state_profiles,
        )
        return self._snapshot

    def current_snapshot(self) -> WorkflowSnapshot:
        """Return the last known good workflow snapshot."""

        assert self._snapshot is not None
        return self._snapshot

    async def run(self, stop_event: asyncio.Event) -> None:
        """Watch for workflow changes with a periodic fallback until shutdown."""

        if self._state is None:
            await self.load_initial_snapshot()
        watch_task = asyncio.create_task(self._watch_loop(stop_event))
        poll_task = asyncio.create_task(self._poll_loop(stop_event))
        try:
            await stop_event.wait()
        finally:
            for task in (watch_task, poll_task):
                task.cancel()
            await asyncio.gather(watch_task, poll_task, return_exceptions=True)

    async def reload_if_changed(self) -> WorkflowSnapshot | None:
        """Reload only when the file stamp changes and keep state untouched on failure."""

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
            state_profiles = parse_state_profiles(
                definition.config, definition.prompt_sections
            )
        except Exception as exc:
            await self._handle_reload_error(exc)
            return None

        version = state.version + 1
        self._state = WorkflowStoreState(
            path=self.path,
            stamp=stamp,
            workflow=definition,
            version=version,
            last_reload_error=None,
        )
        self._snapshot = WorkflowSnapshot(
            version=version,
            path=self.path,
            stamp=stamp,
            definition=definition,
            settings=settings,
            state_profiles=state_profiles,
        )
        await self._publish_snapshot(self._snapshot)
        return self._snapshot

    async def _reload_if_changed(self) -> WorkflowSnapshot | None:
        """Backwards-compatible alias for tests and older callers."""

        return await self.reload_if_changed()

    async def _handle_reload_error(self, error: Any) -> None:
        """Record the reload error while keeping the previous workflow version active."""

        state = self._require_state()
        state.last_reload_error = error
        LOGGER.error(
            "Failed to reload workflow path=%s reason=%r; keeping last known good configuration",
            self.path,
            error,
        )
        await self._publish_error(error)

    async def _poll_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_s)
            except TimeoutError:
                await self._reload_if_changed()

    async def _watch_loop(self, stop_event: asyncio.Event) -> None:
        watch_root = str(Path(self.path).resolve().parent)
        async for _changes in awatch(
            watch_root,
            debounce=self._watch_debounce_ms,
            stop_event=stop_event,
        ):
            await self.reload_if_changed()
            if stop_event.is_set():
                return

    async def _publish_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        for subscriber in self._snapshot_subscribers:
            await subscriber(snapshot)

    async def _publish_error(self, error: Any) -> None:
        for subscriber in self._error_subscribers:
            await subscriber(error)

    def _require_state(self) -> WorkflowStoreState:
        """Internal assertion helper because the actor bootstraps lazily."""

        assert self._state is not None
        return self._state
