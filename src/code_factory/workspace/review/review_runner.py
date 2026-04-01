"""Shared backend orchestration for the operator review flow."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import tempfile
from pathlib import Path

from ...errors import ReviewError
from ...runtime.subprocess import ProcessTree
from ..paths import canonicalize, safe_identifier
from .review_browser import wait_for_http_ready
from .review_models import ReviewTarget, RunningReviewServer
from .review_observer import NullReviewObserver, ReviewObserver
from .review_ports import ensure_review_port_available
from .review_shell import ShellResult, capture_shell
from .review_templates import (
    build_review_environment,
    build_review_launch,
    render_review_template,
    review_context,
)


class ReviewRunner:
    def __init__(
        self,
        *,
        repo_root: str,
        worktree_root: str,
        keep: bool,
        prepare_command: str | None,
    ) -> None:
        self._repo_root = repo_root
        self._worktree_root = worktree_root
        self._keep = keep
        self._prepare_command = prepare_command

    async def run(
        self,
        target: ReviewTarget,
        servers,
        *,
        observer: ReviewObserver | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        active_observer = observer or NullReviewObserver()
        running: list[RunningReviewServer] = []
        log_tasks: list[asyncio.Task[None]] = []
        worktree = _worktree_path(self._worktree_root, target.target)
        try:
            await _create_worktree(self._repo_root, worktree, target)
            head_sha = await _head_sha(worktree)
            await self._run_prepare(active_observer, target, worktree)
            for server in servers:
                entry = await _start_server(target, worktree, head_sha, server)
                running.append(entry)
                active_observer.on_server_started(entry)
                log_tasks.extend(_log_tasks(entry, active_observer))
            active_observer.on_servers_ready(running)
            await _open_review_urls(active_observer, running)
            await _wait_for_exit(running, stop_event=stop_event)
        finally:
            await _stop_servers(running)
            await _cancel_log_tasks(log_tasks)
            await self._cleanup_worktree(active_observer, worktree)

    async def _run_prepare(
        self, observer: ReviewObserver, target: ReviewTarget, worktree: str
    ) -> None:
        if self._prepare_command is None:
            return
        command = render_review_template(
            self._prepare_command,
            review_context(target, worktree, None),
        )
        result = await capture_shell(
            command,
            cwd=worktree,
            env=build_review_environment(target, worktree=worktree, port=None),
        )
        _emit_output(observer, "prepare", result)
        if result.status != 0:
            raise ReviewError(
                f"Prepare command failed for {target.target}: {result.output or result.status}"
            )

    async def _cleanup_worktree(self, observer: ReviewObserver, worktree: str) -> None:
        if self._keep:
            return
        result = await capture_shell(
            f"git worktree remove --force {shlex.quote(worktree)}",
            cwd=self._repo_root,
        )
        if result.status != 0:
            observer.on_warning(
                f"Failed to remove review worktree {worktree}: "
                f"{result.output or result.status}"
            )


def _review_temp_root(configured_root: str | None, repo_root: str) -> str:
    root = configured_root or os.path.join(tempfile.gettempdir(), "code-factory-review")
    return canonicalize(os.path.join(root, Path(repo_root).name))


def _worktree_path(root: str, target: str) -> str:
    return canonicalize(os.path.join(root, safe_identifier(target)))


async def _start_server(target: ReviewTarget, worktree: str, head_sha: str, server):
    launch = build_review_launch(target, worktree, server)
    ensure_review_port_available(target, launch)
    environment = build_review_environment(target, worktree=worktree, port=launch.port)
    process = await ProcessTree.spawn_shell(
        launch.command, cwd=worktree, env=environment
    )
    return RunningReviewServer(
        target=target,
        launch=launch,
        worktree=worktree,
        process=process,
        head_sha=head_sha,
    )


async def _create_worktree(repo_root: str, worktree: str, target: ReviewTarget) -> None:
    os.makedirs(os.path.dirname(worktree), exist_ok=True)
    if os.path.exists(worktree):
        raise ReviewError(f"Review worktree already exists: {worktree}")
    result = await _worktree_add(repo_root, worktree, target.ref)
    if (
        result.status != 0
        and target.branch_name
        and "invalid reference" in (result.output or "").lower()
    ):
        fetch_result = await capture_shell(
            "git fetch origin "
            f"{shlex.quote(f'refs/heads/{target.branch_name}:refs/remotes/origin/{target.branch_name}')}",
            cwd=repo_root,
        )
        if fetch_result.status == 0:
            result = await _worktree_add(repo_root, worktree, target.ref)
    if result.status != 0:
        raise ReviewError(
            f"Failed to create review worktree {worktree}: {result.output or result.status}"
        )


async def _worktree_add(repo_root: str, worktree: str, ref: str) -> ShellResult:
    return await capture_shell(
        f"git worktree add --detach {shlex.quote(worktree)} {shlex.quote(ref)}",
        cwd=repo_root,
    )


async def _head_sha(worktree: str) -> str:
    result = await capture_shell("git rev-parse --short HEAD", cwd=worktree)
    if result.status != 0 or not result.stdout.strip():
        raise ReviewError(f"Failed to resolve HEAD for review worktree {worktree}")
    return result.stdout.strip()


def _emit_output(observer: ReviewObserver, label: str, result: ShellResult) -> None:
    for stream_name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        for line in text.splitlines():
            observer.on_prepare_line(label, stream_name, line)


def _log_tasks(
    entry: RunningReviewServer, observer: ReviewObserver
) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    if entry.process.process.stdout is not None:
        tasks.append(
            asyncio.create_task(
                _stream_output(observer, entry, entry.process.process.stdout, "stdout")
            )
        )
    if entry.process.process.stderr is not None:
        tasks.append(
            asyncio.create_task(
                _stream_output(observer, entry, entry.process.process.stderr, "stderr")
            )
        )
    return tasks


async def _stream_output(
    observer: ReviewObserver, entry: RunningReviewServer, stream, stream_name: str
) -> None:
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            observer.on_server_line(entry, stream_name, text)


async def _wait_for_exit(
    running: list[RunningReviewServer], *, stop_event: asyncio.Event | None
) -> None:
    process_tasks = {
        asyncio.create_task(entry.process.wait()): entry for entry in running
    }
    stop_task = (
        asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    )
    wait_on = set(process_tasks)
    if stop_task is not None:
        wait_on.add(stop_task)
    done, pending = await asyncio.wait(wait_on, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if stop_task is not None and stop_task in done and stop_event is not None:
        return
    finished = next(iter(done))
    status = finished.result()
    entry = process_tasks[finished]
    if status != 0:
        raise ReviewError(
            f"{entry.target.target}:{entry.launch.name} exited with status {status}"
        )
    raise ReviewError(f"{entry.target.target}:{entry.launch.name} exited.")


async def _stop_servers(running: list[RunningReviewServer]) -> None:
    for entry in reversed(running):
        with contextlib.suppress(Exception):
            await entry.process.terminate()


async def _cancel_log_tasks(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _open_review_urls(
    observer: ReviewObserver, running: list[RunningReviewServer]
) -> None:
    for entry in running:
        url = entry.launch.url
        if url is None or not entry.launch.open_browser:
            continue
        ready = await wait_for_http_ready(url)
        if not ready:
            observer.on_warning(
                f"Timed out waiting for {entry.target.target}:{entry.launch.name} "
                f"to respond at {url}; browser was not opened automatically."
            )
            continue
        opened = await asyncio.to_thread(_open_browser, url)
        if not opened:
            observer.on_warning(
                f"Failed to open browser for {entry.target.target}:{entry.launch.name} "
                f"({url})"
            )


def _open_browser(url: str) -> bool:
    import webbrowser

    return webbrowser.open(url)
