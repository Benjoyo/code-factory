"""Helpers for orchestrator-owned workpad autosync."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Collection
from pathlib import Path

from watchfiles import awatch

from ...workspace.workpad import workpad_content_hash
from ..messages import WorkpadHydrated
from ..worker.workpad import sync_workspace_workpad
from .context import OrchestratorContext

LOGGER = logging.getLogger(__name__)
WORKPAD_AUTOSYNC_DEBOUNCE_S = 10.0
WORKPAD_WATCH_DEBOUNCE_MS = 150


async def start_workpad_autosync(
    context: OrchestratorContext, message: WorkpadHydrated
) -> None:
    """Start or replace the autosync watcher for one running issue."""

    entry = context.running.get(message.issue_id)
    if entry is None:
        return
    await stop_workpad_autosync(context, message.issue_id, flush=False)
    entry.workpad_path = message.workpad_path
    entry.workpad_last_synced_hash = message.content_hash
    entry.workpad_watch_task = asyncio.create_task(
        _watch_workpad(context, message.issue_id, message.workpad_path)
    )


async def stop_workpad_autosync(
    context: OrchestratorContext, issue_id: str, *, flush: bool
) -> None:
    """Stop autosync tasks and optionally flush the latest dirty workpad state."""

    entry = context.running.get(issue_id)
    if entry is None:
        return
    tasks = [
        _clear_task(entry, "workpad_debounce_task"),
        _clear_task(entry, "workpad_watch_task"),
        _clear_task(entry, "workpad_sync_task"),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    if flush:
        await _sync_if_dirty(context, issue_id)
    entry.workpad_watch_task = None
    entry.workpad_debounce_task = None
    entry.workpad_sync_task = None


async def _watch_workpad(
    context: OrchestratorContext, issue_id: str, workpad_path: str
) -> None:
    watch_root = str(Path(workpad_path).parent)
    try:
        async for changes in awatch(
            watch_root,
            debounce=WORKPAD_WATCH_DEBOUNCE_MS,
        ):
            if _matching_change(changes, workpad_path):
                _schedule_debounced_sync(context, issue_id)
    except asyncio.CancelledError:
        raise
    except FileNotFoundError:
        return
    except Exception:
        LOGGER.exception(
            "workpad autosync watcher failed issue_id=%s workpad=%s",
            issue_id,
            workpad_path,
        )


def _schedule_debounced_sync(context: OrchestratorContext, issue_id: str) -> None:
    entry = context.running.get(issue_id)
    if entry is None:
        return
    existing = entry.workpad_debounce_task
    if existing is not None and not existing.done():
        existing.cancel()
    entry.workpad_debounce_task = asyncio.create_task(
        _debounce_then_sync(context, issue_id)
    )


async def _debounce_then_sync(context: OrchestratorContext, issue_id: str) -> None:
    try:
        await asyncio.sleep(WORKPAD_AUTOSYNC_DEBOUNCE_S)
        entry = context.running.get(issue_id)
        if entry is None:
            return
        if entry.workpad_debounce_task is asyncio.current_task():
            entry.workpad_debounce_task = None
        entry.workpad_sync_task = asyncio.current_task()
        await _sync_if_dirty(context, issue_id)
    except asyncio.CancelledError:
        raise
    finally:
        entry = context.running.get(issue_id)
        if entry is not None:
            if entry.workpad_debounce_task is asyncio.current_task():
                entry.workpad_debounce_task = None
            if entry.workpad_sync_task is asyncio.current_task():
                entry.workpad_sync_task = None


async def _sync_if_dirty(context: OrchestratorContext, issue_id: str) -> bool:
    entry = context.running.get(issue_id)
    if entry is None or entry.workpad_path is None:
        return False
    content_hash = workpad_content_hash(entry.workpad_path)
    if content_hash is None or content_hash == entry.workpad_last_synced_hash:
        return False
    try:
        await sync_workspace_workpad(
            context.workflow_snapshot.settings,
            context.tracker,
            entry.issue,
            entry.workspace_path,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception(
            "workpad autosync failed issue_id=%s workspace=%s workpad=%s",
            issue_id,
            entry.workspace_path,
            entry.workpad_path,
        )
        return False
    entry.workpad_last_synced_hash = content_hash
    return True


async def _clear_task(entry, name: str) -> None:
    task = getattr(entry, name)
    setattr(entry, name, None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _matching_change(
    changes: Collection[tuple[object, str]], workpad_path: str
) -> bool:
    target = str(Path(workpad_path).resolve())
    for _change, changed_path in changes:
        if str(Path(changed_path).resolve()) == target:
            return True
    return False
