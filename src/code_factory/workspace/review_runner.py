"""Foreground operator workflow for review worktrees and dev servers."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import tempfile
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ..config import parse_settings
from ..errors import ReviewError
from ..runtime.subprocess import ProcessTree
from ..workflow.loader import load_workflow
from .paths import canonicalize, safe_identifier
from .review_browser import wait_for_http_ready
from .review_models import ReviewTarget, RunningReviewServer
from .review_resolution import resolve_repo_root, resolve_review_targets
from .review_shell import capture_shell
from .review_templates import (
    build_review_environment,
    build_review_launch,
    render_review_template,
    review_context,
)


async def run_review_session(
    workflow_path: str,
    targets: list[str],
    *,
    keep: bool,
    console: Console | None = None,
) -> None:
    active_console = console or Console()
    settings = parse_settings(load_workflow(workflow_path).config)
    if not settings.review.servers:
        raise ReviewError("`review.servers` must be configured in WORKFLOW.md.")
    repo_root = await resolve_repo_root(workflow_path)
    resolved_targets = await resolve_review_targets(repo_root, settings, targets)
    worktree_root = _review_temp_root(settings.review.temp_root, repo_root)
    runner = ReviewRunner(
        repo_root=repo_root,
        worktree_root=worktree_root,
        keep=keep,
        prepare_command=settings.review.prepare,
        console=active_console,
    )
    await runner.run(resolved_targets, settings.review.servers)


class ReviewRunner:
    def __init__(
        self,
        *,
        repo_root: str,
        worktree_root: str,
        keep: bool,
        prepare_command: str | None,
        console: Console,
    ) -> None:
        self._repo_root = repo_root
        self._worktree_root = worktree_root
        self._keep = keep
        self._prepare_command = prepare_command
        self._console = console

    async def run(self, targets: list[ReviewTarget], servers) -> None:
        running: list[RunningReviewServer] = []
        created_worktrees: list[str] = []
        log_tasks: list[asyncio.Task[None]] = []
        try:
            for target in targets:
                worktree = _worktree_path(self._worktree_root, target.target)
                await _create_worktree(self._repo_root, worktree, target)
                created_worktrees.append(worktree)
                head_sha = await _head_sha(worktree)
                await self._run_prepare(target, worktree)
                for server in servers:
                    launch = build_review_launch(target, worktree, server)
                    environment = build_review_environment(
                        target, worktree=worktree, port=launch.port
                    )
                    process = await ProcessTree.spawn_shell(
                        launch.command,
                        cwd=worktree,
                        env=environment,
                    )
                    entry = RunningReviewServer(
                        target=target,
                        launch=launch,
                        worktree=worktree,
                        process=process,
                        head_sha=head_sha,
                    )
                    running.append(entry)
                    log_tasks.extend(_log_tasks(self._console, entry))
            _print_summary(self._console, running)
            await _open_review_urls(self._console, running)
            await _wait_for_exit(running)
        finally:
            await _stop_servers(running)
            await _cancel_log_tasks(log_tasks)
            await self._cleanup_worktrees(created_worktrees)

    async def _run_prepare(self, target: ReviewTarget, worktree: str) -> None:
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
        _emit_prefixed_output(
            self._console, f"{target.target}:prepare", result.stdout, result.stderr
        )
        if result.status != 0:
            raise ReviewError(
                f"Prepare command failed for {target.target}: {result.output or result.status}"
            )

    async def _cleanup_worktrees(self, worktrees: list[str]) -> None:
        if self._keep:
            return
        for worktree in reversed(worktrees):
            result = await capture_shell(
                f"git worktree remove --force {shlex.quote(worktree)}",
                cwd=self._repo_root,
            )
            if result.status != 0:
                self._console.print(
                    f"[warn]Failed to remove review worktree {worktree}: "
                    f"{result.output or result.status}[/warn]"
                )


def _review_temp_root(configured_root: str | None, repo_root: str) -> str:
    root = configured_root or os.path.join(tempfile.gettempdir(), "code-factory-review")
    return canonicalize(os.path.join(root, Path(repo_root).name))


def _worktree_path(root: str, target: str) -> str:
    return canonicalize(os.path.join(root, safe_identifier(target)))


async def _create_worktree(
    repo_root: str,
    worktree: str,
    target: ReviewTarget,
) -> None:
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


def _log_tasks(
    console: Console, entry: RunningReviewServer
) -> list[asyncio.Task[None]]:
    label = f"{entry.target.target}:{entry.launch.name}"
    stdout = entry.process.process.stdout
    stderr = entry.process.process.stderr
    tasks: list[asyncio.Task[None]] = []
    if stdout is not None:
        tasks.append(
            asyncio.create_task(_stream_output(console, label, stdout, "stdout"))
        )
    if stderr is not None:
        tasks.append(
            asyncio.create_task(_stream_output(console, label, stderr, "stderr"))
        )
    return tasks


async def _stream_output(
    console: Console, label: str, stream, stream_name: str
) -> None:
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            console.print(
                f"[{label}:{stream_name}] {text}", markup=False, highlight=False
            )


async def _wait_for_exit(running: list[RunningReviewServer]) -> None:
    tasks = {asyncio.create_task(entry.process.wait()): entry for entry in running}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    finished = next(iter(done))
    status = finished.result()
    entry = tasks[finished]
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


def _print_summary(console: Console, running: list[RunningReviewServer]) -> None:
    table = Table(title="Code Factory Review")
    table.add_column("Target")
    table.add_column("Server")
    table.add_column("PID")
    table.add_column("Port")
    table.add_column("URL", overflow="fold")
    table.add_column("Ref")
    table.add_column("PR", overflow="fold")
    table.add_column("Path", overflow="fold")
    for entry in running:
        table.add_row(
            entry.target.target,
            entry.launch.name,
            str(entry.process.pid or ""),
            str(entry.launch.port or ""),
            entry.launch.url or "",
            entry.head_sha,
            entry.target.pr_url or "",
            entry.worktree,
        )
    console.print(table)


async def _open_review_urls(
    console: Console, running: list[RunningReviewServer]
) -> None:
    for entry in running:
        url = entry.launch.url
        if url is None or not entry.launch.open_browser:
            continue
        ready = await wait_for_http_ready(url)
        if not ready:
            console.print(
                f"[warn]Timed out waiting for {entry.target.target}:{entry.launch.name} "
                f"to respond at {url}; browser was not opened automatically.[/warn]"
            )
            continue
        opened = await asyncio.to_thread(webbrowser.open, url)
        if not opened:
            console.print(
                f"[warn]Failed to open browser for {entry.target.target}:{entry.launch.name} "
                f"({url})[/warn]"
            )


def _emit_prefixed_output(
    console: Console,
    label: str,
    stdout: str,
    stderr: str,
) -> None:
    for stream_name, text in (("stdout", stdout), ("stderr", stderr)):
        for line in text.splitlines():
            console.print(
                f"[{label}:{stream_name}] {line}",
                markup=False,
                highlight=False,
            )
