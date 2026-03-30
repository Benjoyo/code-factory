from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from rich.console import Console

from code_factory.config import parse_settings
from code_factory.config.models import ReviewServerSettings
from code_factory.errors import ConfigValidationError, ReviewError
from code_factory.trackers.memory import MemoryTracker
from code_factory.workflow.loader import load_workflow
from code_factory.workspace.paths import canonicalize
from code_factory.workspace.review_browser import wait_for_http_ready
from code_factory.workspace.review_models import ReviewTarget
from code_factory.workspace.review_resolution import (
    dedupe_review_targets,
    resolve_review_targets,
    trailing_ticket_number,
)
from code_factory.workspace.review_runner import ReviewRunner, _create_worktree
from code_factory.workspace.review_shell import ShellResult
from code_factory.workspace.review_templates import (
    build_review_environment,
    build_review_launch,
)

from .conftest import make_issue, write_workflow_file


def test_review_config_parses_and_validates(tmp_path: Path) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        review={
            "temp_root": str(tmp_path / "review-root"),
            "prepare": "pnpm install",
            "servers": [
                {
                    "name": "web",
                    "command": "pnpm dev --port {{ review.port }}",
                    "base_port": 3000,
                    "url": "http://127.0.0.1:{{ review.port }}",
                }
            ],
        },
    )
    settings = parse_settings(load_workflow(str(workflow)).config)
    assert settings.review.temp_root == str((tmp_path / "review-root").resolve())
    assert settings.review.prepare == "pnpm install"
    assert settings.review.servers[0].base_port == 3000
    assert settings.review.servers[0].open_browser is None

    broken = write_workflow_file(
        tmp_path / "BROKEN.md",
        review={"servers": [{"name": "web", "command": ""}]},
    )
    with pytest.raises(ConfigValidationError, match="can't be blank"):
        parse_settings(load_workflow(str(broken)).config)


@pytest.mark.asyncio
async def test_review_resolution_prefers_pr_head_and_dedupes_targets() -> None:
    tracker = MemoryTracker(
        [make_issue(identifier="ENG-12", branch_name="codex/eng-12")]
    )

    async def fake_capture(
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        if command == "git fetch origin":
            return ShellResult(0, "", "")
        if command == "gh auth status":
            return ShellResult(0, "", "")
        if command.startswith("gh pr list"):
            return ShellResult(
                0,
                '[{"number":12,"url":"https://example/pr/12","headRefOid":"abc123"}]',
                "",
            )
        if command.startswith("git symbolic-ref"):
            return ShellResult(0, "origin/main\n", "")
        raise AssertionError(command)

    settings = parse_settings(
        {
            "failure_state": "Human Review",
            "tracker": {"kind": "memory"},
            "states": {"In Progress": {"prompt": "default"}},
            "review": {
                "servers": [{"name": "web", "command": "pnpm dev", "base_port": 3000}]
            },
        }
    )
    resolved = await resolve_review_targets(
        "/repo",
        settings,
        ["main", "ENG-12", "main", "ENG-12"],
        tracker_factory=lambda _settings: tracker,
        shell_capture=fake_capture,
    )
    assert dedupe_review_targets(["main", "ENG-12", "main"]) == ["main", "ENG-12"]
    assert trailing_ticket_number("ENG-12") == 12
    assert resolved[0].ref == "origin/main"
    assert resolved[1].ref == "abc123"
    assert resolved[1].pr_url == "https://example/pr/12"


@pytest.mark.asyncio
async def test_review_resolution_main_fetch_failure_is_fatal() -> None:
    tracker = MemoryTracker([])

    async def fake_capture(
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        if command == "git fetch origin":
            return ShellResult(1, "", "fetch failed")
        raise AssertionError(command)

    settings = parse_settings(
        {
            "failure_state": "Human Review",
            "tracker": {"kind": "memory"},
            "states": {"In Progress": {"prompt": "default"}},
            "review": {
                "servers": [{"name": "web", "command": "pnpm dev", "base_port": 3000}]
            },
        }
    )
    with pytest.raises(ReviewError, match="Failed to fetch origin"):
        await resolve_review_targets(
            "/repo",
            settings,
            ["main"],
            tracker_factory=lambda _settings: tracker,
            shell_capture=fake_capture,
        )


def test_review_template_environment_and_ports() -> None:
    target = ReviewTarget(
        target="ENG-12",
        kind="ticket",
        ticket_identifier="ENG-12",
        ticket_number=12,
        ref="abc123",
    )
    server = ReviewServerSettings(
        name="web",
        command="pnpm dev --port {{ review.port }}",
        base_port=3000,
        url="http://127.0.0.1:{{ review.port }}",
    )
    launch = build_review_launch(target, "/tmp/worktree", server)
    env = build_review_environment(target, worktree="/tmp/worktree", port=launch.port)
    assert launch.command == "pnpm dev --port 3012"
    assert launch.url == "http://127.0.0.1:3012"
    assert launch.open_browser is True
    assert env["CF_REVIEW_TICKET_NUMBER"] == "12"
    assert env["CF_REVIEW_PORT"] == "3012"
    disabled_launch = build_review_launch(
        target,
        "/tmp/worktree",
        ReviewServerSettings(
            name="api",
            command="uv run api",
            base_port=8000,
            url="http://127.0.0.1:{{ review.port }}",
            open_browser=False,
        ),
    )
    assert disabled_launch.open_browser is False
    no_url_launch = build_review_launch(
        target,
        "/tmp/worktree",
        ReviewServerSettings(name="worker", command="uv run worker"),
    )
    assert no_url_launch.url is None
    assert no_url_launch.open_browser is False
    with pytest.raises(ReviewError, match="does not end with digits"):
        build_review_launch(
            ReviewTarget(
                target="ENG-X",
                kind="ticket",
                ticket_identifier="ENG-X",
                ticket_number=None,
                ref="abc123",
            ),
            "/tmp/worktree",
            server,
        )


@pytest.mark.asyncio
async def test_review_runner_runs_prepare_and_cleans_up(monkeypatch) -> None:
    console_io = io.StringIO()
    console = Console(
        file=console_io,
        force_terminal=False,
        color_system=None,
        width=200,
    )
    calls: list[tuple[str, str]] = []
    spawn_envs: list[dict[str, str] | None] = []
    opened_urls: list[str] = []

    async def fake_create_worktree(
        repo_root: str, worktree: str, target: ReviewTarget
    ) -> None:
        calls.append(("create", worktree))

    async def fake_head_sha(worktree: str) -> str:
        return "abc123"

    async def fake_capture_shell(
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        calls.append((command, cwd))
        return ShellResult(0, "prepared\n" if command == "echo prep" else "", "")

    class FakeStream:
        def __init__(self, *lines: bytes) -> None:
            self._lines = list(lines) + [b""]

        async def readline(self) -> bytes:
            return self._lines.pop(0)

    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 4321
            self.returncode = None
            self.stdout = FakeStream(b"ready\n")
            self.stderr = FakeStream()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        async def terminate(self) -> None:
            self.returncode = 0

    async def fake_spawn_shell(_cls, command: str, **kwargs):
        from code_factory.runtime.subprocess.process_tree import ProcessTree

        spawn_envs.append(kwargs.get("env"))
        return ProcessTree(
            process=cast(Any, FakeProcess()),
            command=command,
            cwd=kwargs["cwd"],
        )

    monkeypatch.setattr(
        "code_factory.workspace.review_runner._create_worktree",
        fake_create_worktree,
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner._head_sha",
        fake_head_sha,
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner.capture_shell",
        fake_capture_shell,
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner.ProcessTree.spawn_shell",
        classmethod(fake_spawn_shell),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner.webbrowser.open",
        lambda url: opened_urls.append(url) or True,
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner.wait_for_http_ready",
        lambda _url: asyncio.sleep(0, result=True),
    )
    runner = ReviewRunner(
        repo_root="/repo",
        worktree_root="/tmp/review-root",
        keep=False,
        prepare_command="echo prep",
        console=console,
    )
    with pytest.raises(ReviewError, match="exited"):
        await runner.run(
            [
                ReviewTarget(
                    target="ENG-12",
                    kind="ticket",
                    ticket_identifier="ENG-12",
                    ticket_number=12,
                    ref="abc123",
                    pr_url="https://example/pr/12",
                )
            ],
            (
                ReviewServerSettings(
                    name="web",
                    command="pnpm dev --port {{ review.port }}",
                    base_port=3000,
                    url="http://127.0.0.1:{{ review.port }}",
                ),
            ),
        )
    worktree = canonicalize("/tmp/review-root/ENG-12")
    assert ("create", worktree) in calls
    assert ("echo prep", worktree) in calls
    assert (
        f"git worktree remove --force {worktree}",
        "/repo",
    ) in calls
    assert spawn_envs[0] is not None
    assert spawn_envs[0]["CF_REVIEW_PORT"] == "3012"
    assert opened_urls == ["http://127.0.0.1:3012"]
    output = console_io.getvalue()
    assert "Code Factory Review" in output
    assert "https://example/pr/12" in output


@pytest.mark.asyncio
async def test_review_runner_skips_browser_when_disabled(monkeypatch) -> None:
    opened_urls: list[str] = []

    monkeypatch.setattr(
        "code_factory.workspace.review_runner._create_worktree",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner._head_sha",
        lambda _worktree: asyncio.sleep(0, result="abc123"),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner.capture_shell",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=ShellResult(0, "", "")),
    )

    class FakeProcess:
        pid = 4321
        returncode = None

        class _Stream:
            async def readline(self) -> bytes:
                return b""

        stdout = _Stream()
        stderr = _Stream()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        async def terminate(self) -> None:
            self.returncode = 0

    async def fake_spawn_shell(_cls, command: str, **kwargs):
        from code_factory.runtime.subprocess.process_tree import ProcessTree

        return ProcessTree(
            process=cast(Any, FakeProcess()),
            command=command,
            cwd=kwargs["cwd"],
        )

    monkeypatch.setattr(
        "code_factory.workspace.review_runner.ProcessTree.spawn_shell",
        classmethod(fake_spawn_shell),
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner.webbrowser.open",
        lambda url: opened_urls.append(url) or True,
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_runner.wait_for_http_ready",
        lambda _url: asyncio.sleep(0, result=True),
    )
    runner = ReviewRunner(
        repo_root="/repo",
        worktree_root="/tmp/review-root",
        keep=True,
        prepare_command=None,
        console=Console(file=io.StringIO(), force_terminal=False, color_system=None),
    )
    with pytest.raises(ReviewError, match="exited"):
        await runner.run(
            [
                ReviewTarget(
                    target="ENG-12",
                    kind="ticket",
                    ticket_identifier="ENG-12",
                    ticket_number=12,
                    ref="abc123",
                )
            ],
            (
                ReviewServerSettings(
                    name="web",
                    command="pnpm dev --port {{ review.port }}",
                    base_port=3000,
                    url="http://127.0.0.1:{{ review.port }}",
                    open_browser=False,
                ),
            ),
        )
    assert opened_urls == []


@pytest.mark.asyncio
async def test_create_worktree_fetches_ticket_branch_when_pr_head_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = str(tmp_path / "review" / "BEN-24")
    calls: list[tuple[str, str]] = []

    async def fake_capture_shell(
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        calls.append((command, cwd))
        if command.startswith("git worktree add --detach") and len(calls) == 1:
            return ShellResult(128, "", "fatal: invalid reference: deadbeef")
        if command.startswith("git fetch origin "):
            return ShellResult(0, "", "")
        if command.startswith("git worktree add --detach"):
            return ShellResult(0, "prepared\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.review_runner.capture_shell",
        fake_capture_shell,
    )

    await _create_worktree(
        "/repo",
        worktree,
        ReviewTarget(
            target="BEN-24",
            kind="ticket",
            ticket_identifier="BEN-24",
            ticket_number=24,
            ref="deadbeef",
            branch_name="codex/ben-24",
            head_sha="deadbeef",
        ),
    )
    assert calls == [
        (f"git worktree add --detach {worktree} deadbeef", "/repo"),
        (
            "git fetch origin refs/heads/codex/ben-24:refs/remotes/origin/codex/ben-24",
            "/repo",
        ),
        (f"git worktree add --detach {worktree} deadbeef", "/repo"),
    ]


@pytest.mark.asyncio
async def test_wait_for_http_ready_retries_until_response() -> None:
    attempts = 0

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("not ready")
            return httpx.Response(404, request=httpx.Request("GET", url))

    assert (
        await wait_for_http_ready(
            "http://127.0.0.1:3000",
            timeout_s=0.05,
            interval_s=0,
            client_factory=FakeClient,
        )
        is True
    )
    assert attempts == 3


@pytest.mark.asyncio
async def test_wait_for_http_ready_times_out_when_server_never_responds() -> None:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            raise httpx.ConnectError("not ready")

    assert (
        await wait_for_http_ready(
            "http://127.0.0.1:3000",
            timeout_s=0,
            interval_s=0,
            client_factory=FakeClient,
        )
        is False
    )


@pytest.mark.asyncio
async def test_review_runner_keep_skips_cleanup(monkeypatch) -> None:
    async def fake_capture_shell(
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        raise AssertionError(command)

    monkeypatch.setattr(
        "code_factory.workspace.review_runner.capture_shell",
        fake_capture_shell,
    )
    runner = ReviewRunner(
        repo_root="/repo",
        worktree_root="/tmp/review-root",
        keep=True,
        prepare_command=None,
        console=Console(file=io.StringIO(), force_terminal=False, color_system=None),
    )
    await runner._cleanup_worktrees(["/tmp/review-root/ENG-12"])
