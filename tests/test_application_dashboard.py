from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rich.console import Console

from code_factory.application.dashboard import (
    LiveStatusDashboard,
    StatusDashboardContext,
    _rolling_tps,
    _total_tokens,
    dashboard_url,
    project_url,
    render_status_dashboard,
)
from code_factory.application.dashboard_format import (
    clean_inline,
    int_value,
    mapping_list,
    next_refresh_text,
    pick,
    rate_limit_bucket,
    rate_limit_credits,
    rate_limits_text,
)
from code_factory.application.dashboard_render import _event_style


def test_dashboard_urls_match_elixir_style_host_rules() -> None:
    assert project_url("labelforge-studio") == (
        "https://linear.app/project/labelforge-studio/issues"
    )
    assert dashboard_url("0.0.0.0", 4000) == "http://127.0.0.1:4000/"
    assert dashboard_url("::1", 4000) == "http://[::1]:4000/"
    assert dashboard_url("127.0.0.1", None) is None


def test_render_status_dashboard_includes_summary_running_and_backoff_sections() -> (
    None
):
    snapshot = {
        "running": [
            {
                "identifier": "ENG-1",
                "state": "In Progress",
                "runtime_pid": "12345",
                "runtime_seconds": 126,
                "turn_count": 3,
                "total_tokens": 9876,
                "session_id": "abcd1234-turn-42",
                "last_agent_event": "turn_completed",
                "last_agent_message": {"message": "Finished test pass and lint"},
            }
        ],
        "retrying": [
            {
                "identifier": "ENG-2",
                "attempt": 2,
                "due_in_ms": 3042,
                "error": "tracker timeout",
            }
        ],
        "agent_totals": {
            "input_tokens": 16941792,
            "output_tokens": 53508,
            "total_tokens": 16995300,
            "seconds_running": 2400,
        },
        "rate_limits": {
            "limit_id": "gpt-5",
            "primary": {"remaining": 0, "limit": 20000, "reset_in_seconds": 95},
            "secondary": {"remaining": 0, "limit": 60, "resetInSeconds": 45},
            "credits": {},
        },
        "polling": {"next_poll_in_ms": 3000, "checking?": False},
    }
    context = StatusDashboardContext(
        max_agents=2,
        project_url="https://linear.app/project/labelforge-studio/issues",
        dashboard_url="http://127.0.0.1:4000/",
    )
    stream = StringIO()
    Console(file=stream, width=140).print(
        render_status_dashboard(snapshot, context, throughput_tps=15)
    )
    rendered = stream.getvalue()

    assert "SYMPHONY STATUS" in rendered
    assert "Agents:" in rendered and "1/2" in rendered
    assert "Throughput:" in rendered and "15 tps" in rendered
    assert "Runtime:" in rendered and "42m 6s" in rendered
    assert "Tokens:" in rendered and "16,995,300" in rendered
    assert "Rate Limits:" in rendered and "primary 0/20,000 reset 95s" in rendered
    assert "Project:" in rendered and "labelforge-studio/issues" in rendered
    assert "Dashboard:" in rendered and "127.0.0.1:4000" in rendered
    assert "Next refresh:" in rendered and "3s" in rendered
    assert "Running" in rendered and "ENG-1" in rendered
    assert "Backoff queue" in rendered and "ENG-2" in rendered


def test_render_status_dashboard_covers_unavailable_and_empty_sections() -> None:
    stream = StringIO()
    Console(file=stream, width=120).print(
        render_status_dashboard(
            {},
            StatusDashboardContext(max_agents=1, project_url=None, dashboard_url=None),
            throughput_tps=0,
            unavailable=True,
            unavailable_detail="RuntimeError: boom",
        )
    )
    rendered = stream.getvalue()
    assert "Orchestrator snapshot unavailable" in rendered
    assert "RuntimeError: boom" in rendered

    empty_stream = StringIO()
    Console(file=empty_stream, width=120).print(
        render_status_dashboard(
            {
                "running": [],
                "retrying": [],
                "agent_totals": {},
                "rate_limits": "weird",
                "polling": {"checking?": True},
            },
            StatusDashboardContext(max_agents=3, project_url=None, dashboard_url=None),
            throughput_tps=0,
        )
    )
    empty_rendered = empty_stream.getvalue()
    assert "No active agents" in empty_rendered
    assert "No queued retries" in empty_rendered
    assert "checking now..." in empty_rendered
    assert "weird" in empty_rendered

    retry_stream = StringIO()
    Console(file=retry_stream, width=120).print(
        render_status_dashboard(
            {
                "running": [],
                "retrying": [{"identifier": "ENG-9", "attempt": 1, "due_in_ms": 5}],
                "agent_totals": {},
                "rate_limits": None,
                "polling": {},
            },
            StatusDashboardContext(max_agents=1, project_url=None, dashboard_url=None),
            throughput_tps=0,
        )
    )
    assert "error=" not in retry_stream.getvalue()


def test_dashboard_format_helpers_cover_remaining_branches() -> None:
    assert next_refresh_text({"checking?": True}).plain == "checking now..."
    assert next_refresh_text({"next_poll_in_ms": 1500}).plain == "2s"
    assert next_refresh_text(None).plain == "n/a"

    assert rate_limits_text(None).plain == "unavailable"
    assert rate_limits_text("odd").plain == "odd"
    assert "reset 2s" in rate_limit_bucket(
        {"remaining": 1, "limit": 2, "reset_in_seconds": 2}
    )
    assert "reset soon" in rate_limit_bucket(
        {"remaining": "3", "limit": "4", "resetAt": "soon"}
    )
    assert rate_limit_bucket({"remaining": 1, "limit": 2, "resetAt": ""}) == "1/2"
    assert rate_limit_bucket([]) == "n/a"
    assert rate_limit_credits(None) == "credits n/a"
    assert rate_limit_credits({"unlimited": True}) == "credits unlimited"
    assert rate_limit_credits({}) == "credits none"
    assert rate_limit_credits({"remaining": 12}) == "credits 12"
    assert rate_limit_credits("7.5") == "credits 7.5"
    assert mapping_list([{"ok": True}, 1]) == [{"ok": True}]
    assert mapping_list(None) == []
    assert pick({"a": 1}, "b", "a") == 1
    assert pick(None, "a") is None
    assert int_value(" 9 ") == 9
    assert int_value(2.7) == 2
    assert int_value(object()) == 0
    assert clean_inline("a\nb\tc", 5) == "a b c"


def test_dashboard_helpers_cover_token_and_event_branches() -> None:
    assert _total_tokens({"agent_totals": {"total_tokens": 7}}) == 7
    assert _total_tokens({"agent_totals": {"total_tokens": 7.9}}) == 7
    assert _total_tokens({"agent_totals": {"total_tokens": "8"}}) == 8
    assert _total_tokens({"agent_totals": {"total_tokens": "bad"}}) == 0
    assert _total_tokens({"agent_totals": []}) == 0

    assert _rolling_tps([], 1000, 0) == 0
    assert _rolling_tps([(1000, 10), (1000, 10)], 1000, 10) == 0
    assert _rolling_tps([(1000, 10), (2000, 20)], 2000, 20) == 10

    assert _event_style("tool_failed", stopping=False) == "red"
    assert _event_style("turn_completed", stopping=False) == "magenta"
    assert _event_style("task_started", stopping=False) == "green"
    assert _event_style("token_count", stopping=False) == "yellow"
    assert _event_style("other", stopping=False) == "bright_blue"


def test_dashboard_enabled_checks_tty() -> None:
    settings = SimpleNamespace(
        observability=SimpleNamespace(dashboard_enabled=True, refresh_ms=1000)
    )
    assert (
        LiveStatusDashboard.enabled(
            cast(Any, settings), SimpleNamespace(isatty=lambda: True)
        )
        is True
    )
    assert (
        LiveStatusDashboard.enabled(
            cast(Any, settings), SimpleNamespace(isatty=lambda: False)
        )
        is False
    )
    settings.observability.dashboard_enabled = False
    assert (
        LiveStatusDashboard.enabled(
            cast(Any, settings), SimpleNamespace(isatty=lambda: True)
        )
        is False
    )


class _FakeLive:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.updates: list[tuple[Any, bool]] = []

    def __enter__(self) -> _FakeLive:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def update(self, renderable: Any, refresh: bool = False) -> None:
        self.updates.append((renderable, refresh))


def _dashboard_settings() -> Any:
    return SimpleNamespace(observability=SimpleNamespace(refresh_ms=250))


@pytest.mark.asyncio
async def test_live_status_dashboard_run_and_snapshot_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_live = _FakeLive()
    stop_event = asyncio.Event()
    real_wait_for = asyncio.wait_for
    wait_for_calls = 0

    class SnapshotNowOrchestrator:
        def snapshot_now(self) -> dict[str, Any]:
            return {
                "running": [],
                "retrying": [],
                "agent_totals": {"total_tokens": "11"},
            }

    monkeypatch.setattr(
        "code_factory.application.dashboard.Live", lambda *a, **k: fake_live
    )
    dashboard = LiveStatusDashboard(
        cast(Any, SnapshotNowOrchestrator()),
        settings=_dashboard_settings(),
        context=StatusDashboardContext(
            max_agents=1, project_url=None, dashboard_url=None
        ),
    )

    async def stop_after_first_wait(awaitable: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal wait_for_calls
        wait_for_calls += 1
        if wait_for_calls == 1:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            stop_event.set()
            raise TimeoutError
        return await real_wait_for(awaitable, *args, **kwargs)

    monkeypatch.setattr("asyncio.wait_for", stop_after_first_wait)
    await dashboard.run(stop_event)
    assert fake_live.updates and fake_live.updates[0][1] is True
    monkeypatch.setattr("asyncio.wait_for", real_wait_for)

    class AsyncSnapshotOrchestrator:
        async def snapshot(self) -> dict[str, Any]:
            return {
                "running": [],
                "retrying": [],
                "agent_totals": {"total_tokens": 5},
            }

    dashboard_async = LiveStatusDashboard(
        cast(Any, AsyncSnapshotOrchestrator()),
        settings=_dashboard_settings(),
        context=StatusDashboardContext(
            max_agents=1, project_url=None, dashboard_url=None
        ),
    )
    assert await dashboard_async._snapshot_renderable() is not None

    class BadSnapshotOrchestrator:
        def snapshot_now(self) -> object:
            return object()

    dashboard_bad = LiveStatusDashboard(
        cast(Any, BadSnapshotOrchestrator()),
        settings=_dashboard_settings(),
        context=StatusDashboardContext(
            max_agents=1, project_url=None, dashboard_url=None
        ),
    )
    await dashboard_bad._snapshot_renderable()
    assert "snapshot payload is not a mapping" in (
        dashboard_bad._last_snapshot_error or ""
    )

    class FailingSnapshotOrchestrator:
        def snapshot_now(self) -> dict[str, Any]:
            raise RuntimeError("boom")

    dashboard_fail = LiveStatusDashboard(
        cast(Any, FailingSnapshotOrchestrator()),
        settings=_dashboard_settings(),
        context=StatusDashboardContext(
            max_agents=1, project_url=None, dashboard_url=None
        ),
    )
    await dashboard_fail._snapshot_renderable()
    assert dashboard_fail._last_snapshot_error == "RuntimeError: boom"
