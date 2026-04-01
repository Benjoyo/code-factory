from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import code_factory.runtime.orchestration.workpad_autosync as autosync
from code_factory.runtime.messages import WorkpadHydrated
from code_factory.runtime.orchestration.actor import OrchestratorActor
from code_factory.runtime.orchestration.models import RunningEntry
from code_factory.runtime.orchestration.workpad_autosync import (
    start_workpad_autosync,
    stop_workpad_autosync,
)
from code_factory.runtime.worker import workpad as worker_workpad
from code_factory.trackers.memory import MemoryTracker
from code_factory.workspace.workpad import workpad_content_hash, workspace_workpad_path

from ..conftest import make_issue, make_snapshot, write_workflow_file


def make_actor(tmp_path: Path, tracker: MemoryTracker) -> OrchestratorActor:
    snapshot = make_snapshot(
        write_workflow_file(tmp_path / "WORKFLOW.md", tracker={"kind": "memory"})
    )
    actor = OrchestratorActor(snapshot, tracker_factory=lambda settings: tracker)
    actor.tracker = tracker
    return actor


class FakeWatch:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()

    def __call__(self, *_args: Any, **_kwargs: Any) -> FakeWatch:
        return self

    def __aiter__(self) -> FakeWatch:
        return self

    async def __anext__(self) -> set[tuple[object, str]]:
        item = await self.queue.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        return item

    async def emit(self, changes: set[tuple[object, str]]) -> None:
        await self.queue.put(changes)


@pytest.mark.asyncio
async def test_workpad_autosync_debounces_and_ignores_unrelated_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-1", identifier="ENG-1", state="In Progress")
    tracker = MemoryTracker([issue])
    actor = make_actor(tmp_path, tracker)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workpad_path = workspace_workpad_path(str(workspace))
    Path(workpad_path).write_text("initial\n", encoding="utf-8")
    actor.running["issue-1"] = RunningEntry(
        issue_id="issue-1",
        identifier="ENG-1",
        issue=issue,
        workspace_path=str(workspace),
        worker=object(),
        started_at=datetime.now(UTC),
    )
    watch = FakeWatch()
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.awatch",
        watch,
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.WORKPAD_AUTOSYNC_DEBOUNCE_S",
        0.01,
    )

    await start_workpad_autosync(
        actor,
        WorkpadHydrated(
            issue_id="issue-1",
            workspace_path=str(workspace),
            workpad_path=str(Path(workpad_path).resolve()),
            content_hash=workpad_content_hash(workpad_path),
        ),
    )

    await watch.emit({(object(), str((workspace / "notes.txt").resolve()))})
    await asyncio.sleep(0.03)
    assert await tracker.fetch_issue_comments(issue.id or "") == []

    Path(workpad_path).write_text("first\n", encoding="utf-8")
    await watch.emit({(object(), str(Path(workpad_path).resolve()))})
    await asyncio.sleep(0.002)
    Path(workpad_path).write_text("second\n", encoding="utf-8")
    await watch.emit({(object(), str(Path(workpad_path).resolve()))})
    await asyncio.sleep(0.05)

    comments = await tracker.fetch_issue_comments(issue.id or "")
    assert len(comments) == 1
    assert comments[0].body == "second\n"
    assert actor.running["issue-1"].workpad_last_synced_hash == workpad_content_hash(
        workpad_path
    )

    await stop_workpad_autosync(actor, "issue-1", flush=False)


@pytest.mark.asyncio
async def test_workpad_autosync_skips_unchanged_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-2", identifier="ENG-2", state="In Progress")
    tracker = MemoryTracker([issue])
    actor = make_actor(tmp_path, tracker)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workpad_path = workspace_workpad_path(str(workspace))
    Path(workpad_path).write_text("steady\n", encoding="utf-8")
    actor.running["issue-2"] = RunningEntry(
        issue_id="issue-2",
        identifier="ENG-2",
        issue=issue,
        workspace_path=str(workspace),
        worker=object(),
        started_at=datetime.now(UTC),
    )
    watch = FakeWatch()
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.awatch",
        watch,
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.WORKPAD_AUTOSYNC_DEBOUNCE_S",
        0.01,
    )

    await start_workpad_autosync(
        actor,
        WorkpadHydrated(
            issue_id="issue-2",
            workspace_path=str(workspace),
            workpad_path=str(Path(workpad_path).resolve()),
            content_hash=workpad_content_hash(workpad_path),
        ),
    )

    await watch.emit({(object(), str(Path(workpad_path).resolve()))})
    await asyncio.sleep(0.03)
    assert await tracker.fetch_issue_comments(issue.id or "") == []

    await stop_workpad_autosync(actor, "issue-2", flush=False)


@pytest.mark.asyncio
async def test_workpad_autosync_retries_after_sync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issue = make_issue(id="issue-3", identifier="ENG-3", state="In Progress")
    tracker = MemoryTracker([issue])
    actor = make_actor(tmp_path, tracker)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workpad_path = workspace_workpad_path(str(workspace))
    Path(workpad_path).write_text("initial\n", encoding="utf-8")
    actor.running["issue-3"] = RunningEntry(
        issue_id="issue-3",
        identifier="ENG-3",
        issue=issue,
        workspace_path=str(workspace),
        worker=object(),
        started_at=datetime.now(UTC),
    )
    watch = FakeWatch()
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.awatch",
        watch,
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.WORKPAD_AUTOSYNC_DEBOUNCE_S",
        0.01,
    )

    real_sync = worker_workpad.sync_workspace_workpad
    calls = 0

    async def flaky_sync(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        await real_sync(*args, **kwargs)

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.sync_workspace_workpad",
        flaky_sync,
    )

    await start_workpad_autosync(
        actor,
        WorkpadHydrated(
            issue_id="issue-3",
            workspace_path=str(workspace),
            workpad_path=str(Path(workpad_path).resolve()),
            content_hash=workpad_content_hash(workpad_path),
        ),
    )

    Path(workpad_path).write_text("dirty\n", encoding="utf-8")
    await watch.emit({(object(), str(Path(workpad_path).resolve()))})
    await asyncio.sleep(0.03)
    assert "workpad autosync failed" in caplog.text
    assert await tracker.fetch_issue_comments(issue.id or "") == []

    await watch.emit({(object(), str(Path(workpad_path).resolve()))})
    await asyncio.sleep(0.03)
    comments = await tracker.fetch_issue_comments(issue.id or "")
    assert calls == 2
    assert len(comments) == 1
    assert comments[0].body == "dirty\n"

    await stop_workpad_autosync(actor, "issue-3", flush=False)


@pytest.mark.asyncio
async def test_workpad_autosync_noops_without_running_entry(tmp_path: Path) -> None:
    actor = make_actor(tmp_path, MemoryTracker([]))
    await start_workpad_autosync(
        actor,
        WorkpadHydrated(
            issue_id="missing",
            workspace_path=str(tmp_path),
            workpad_path=str((tmp_path / "workpad.md").resolve()),
            content_hash=None,
        ),
    )
    await stop_workpad_autosync(actor, "missing", flush=True)
    autosync._schedule_debounced_sync(actor, "missing")


@pytest.mark.asyncio
async def test_workpad_autosync_logs_unexpected_watcher_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenWatch:
        def __call__(self, *_args: Any, **_kwargs: Any) -> BrokenWatch:
            return self

        def __aiter__(self) -> BrokenWatch:
            return self

        async def __anext__(self) -> Any:
            raise RuntimeError("watch boom")

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.awatch",
        BrokenWatch(),
    )

    await autosync._watch_workpad(
        make_actor(tmp_path, MemoryTracker([])),
        "issue-4",
        str((tmp_path / "workspace" / "workpad.md").resolve()),
    )

    assert "workpad autosync watcher failed" in caplog.text


@pytest.mark.asyncio
async def test_workpad_autosync_watcher_can_exit_cleanly_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyWatch:
        def __call__(self, *_args: Any, **_kwargs: Any) -> EmptyWatch:
            return self

        def __aiter__(self) -> EmptyWatch:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.awatch",
        EmptyWatch(),
    )

    await autosync._watch_workpad(
        make_actor(tmp_path, MemoryTracker([])),
        "issue-empty",
        str((tmp_path / "workspace" / "workpad.md").resolve()),
    )


@pytest.mark.asyncio
async def test_debounce_then_sync_handles_missing_entry_and_cancelled_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = make_actor(tmp_path, MemoryTracker([]))
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.WORKPAD_AUTOSYNC_DEBOUNCE_S",
        0.0,
    )

    await autosync._debounce_then_sync(actor, "missing")

    issue = make_issue(id="issue-5", identifier="ENG-5", state="In Progress")
    actor.running["issue-5"] = RunningEntry(
        issue_id="issue-5",
        identifier="ENG-5",
        issue=issue,
        workspace_path=str(tmp_path / "workspace"),
        worker=object(),
        started_at=datetime.now(UTC),
    )
    task = asyncio.create_task(autosync._debounce_then_sync(actor, "issue-5"))
    actor.running["issue-5"].workpad_debounce_task = task
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert actor.running["issue-5"].workpad_debounce_task is None


@pytest.mark.asyncio
async def test_debounce_then_sync_runs_without_owning_debounce_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-55", identifier="ENG-55", state="In Progress")
    tracker = MemoryTracker([issue])
    actor = make_actor(tmp_path, tracker)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workpad_path = workspace_workpad_path(str(workspace))
    Path(workpad_path).write_text("dirty\n", encoding="utf-8")
    actor.running["issue-55"] = RunningEntry(
        issue_id="issue-55",
        identifier="ENG-55",
        issue=issue,
        workspace_path=str(workspace),
        worker=object(),
        started_at=datetime.now(UTC),
        workpad_path=str(Path(workpad_path).resolve()),
        workpad_last_synced_hash="stale",
    )
    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.WORKPAD_AUTOSYNC_DEBOUNCE_S",
        0.0,
    )
    sync_calls: list[str] = []

    async def fake_sync_if_dirty(*_args: Any, **_kwargs: Any) -> bool:
        sync_calls.append("called")
        return True

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync._sync_if_dirty",
        fake_sync_if_dirty,
    )

    await autosync._debounce_then_sync(actor, "issue-55")

    assert sync_calls == ["called"]
    assert actor.running["issue-55"].workpad_sync_task is None


@pytest.mark.asyncio
async def test_sync_if_dirty_propagates_cancelled_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-6", identifier="ENG-6", state="In Progress")
    tracker = MemoryTracker([issue])
    actor = make_actor(tmp_path, tracker)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workpad_path = workspace_workpad_path(str(workspace))
    Path(workpad_path).write_text("dirty\n", encoding="utf-8")
    actor.running["issue-6"] = RunningEntry(
        issue_id="issue-6",
        identifier="ENG-6",
        issue=issue,
        workspace_path=str(workspace),
        worker=object(),
        started_at=datetime.now(UTC),
        workpad_path=str(Path(workpad_path).resolve()),
        workpad_last_synced_hash="stale",
    )

    async def cancelled_sync(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.workpad_autosync.sync_workspace_workpad",
        cancelled_sync,
    )

    with pytest.raises(asyncio.CancelledError):
        await autosync._sync_if_dirty(actor, "issue-6")
