from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from code_factory.coding_agents.codex.app_server.client import AppServerClient
from code_factory.coding_agents.codex.app_server.policies import (
    review_turn_sandbox_policy,
)
from code_factory.coding_agents.codex.app_server.protocol import start_turn
from code_factory.coding_agents.codex.app_server.reviews import (
    DisabledDynamicToolExecutor,
    await_review_completion,
    extract_review_output,
)
from code_factory.coding_agents.codex.app_server.session import AppServerSession
from code_factory.coding_agents.review_models import (
    ReviewCodeLocation,
    ReviewFinding,
    ReviewLineRange,
    ReviewOutput,
    normalize_review_output,
)
from code_factory.errors import AppServerError
from code_factory.prompts.review_assets import review_output_schema
from code_factory.runtime.worker.ai_review import (
    AiReviewPassResult,
    ExecutedAiReview,
    run_ai_review_gate,
)
from code_factory.runtime.worker.completion import run_pre_complete_turns
from code_factory.structured_results import StructuredTurnResult
from code_factory.workspace.ai_review_feedback import ai_review_exhausted_summary
from code_factory.workspace.review_surface import (
    WorktreeReviewSelection,
    WorktreeReviewSurface,
)

from .conftest import make_issue, make_snapshot, write_workflow_file


def make_settings(tmp_path: Path, *, overrides: dict[str, Any] | None = None):
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", **(overrides or {}))
    return make_snapshot(workflow).settings


def make_stream_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    return reader


class FakeProcess:
    def __init__(self, pid: int | None = 123) -> None:
        self.pid = pid
        self.returncode = 0
        self.stdout = make_stream_reader()
        self.stderr = make_stream_reader()
        self.stdin = SimpleNamespace(write=lambda data: None, drain=self._drain)

    async def _drain(self) -> None:
        return None


def make_session() -> AppServerSession:
    process = FakeProcess()
    process_tree = cast(
        Any,
        SimpleNamespace(
            process=process,
            pid=123,
            terminate=_async_noop,
        ),
    )
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    done.set_result(None)
    return AppServerSession(
        process_tree=process_tree,
        workspace="/tmp/workspace",
        approval_policy="never",
        thread_sandbox="workspace-write",
        turn_sandbox_policy={"type": "workspaceWrite"},
        thread_id="thread-1",
        read_timeout_ms=50,
        turn_timeout_ms=50,
        auto_approve_requests=True,
        stdout_queue=asyncio.Queue(),
        event_queue=asyncio.Queue(),
        stdout_task=done,  # type: ignore[arg-type]
        stderr_task=done,  # type: ignore[arg-type]
        wait_task=done,  # type: ignore[arg-type]
    )


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _review_output(*, title: str = "Finding", confidence: float = 0.9) -> ReviewOutput:
    return ReviewOutput(
        findings=(
            ReviewFinding(
                title=title,
                body=f"{title} details",
                code_location=ReviewCodeLocation(
                    absolute_file_path="/tmp/workspace/app.py",
                    line_range=ReviewLineRange(start=4, end=5),
                ),
                confidence_score=confidence,
                priority=1,
            ),
        ),
        overall_correctness="incorrect",
        overall_explanation="Needs a fix.",
        overall_confidence_score=0.82,
    )


def test_review_models_and_feedback_edge_cases() -> None:
    assert normalize_review_output(None) is None
    assert normalize_review_output({"findings": "bad"}) is None
    assert (
        normalize_review_output(
            {
                "findings": ["bad"],
                "overall_correctness": "incorrect",
                "overall_explanation": "x",
                "overall_confidence_score": 0.7,
            }
        )
        is None
    )
    assert (
        normalize_review_output(
            {
                "findings": [{}],
                "overall_correctness": "incorrect",
                "overall_explanation": "x",
                "overall_confidence_score": 0.7,
            }
        )
        is None
    )
    assert (
        normalize_review_output(
            {
                "findings": [
                    {
                        "title": "bad",
                        "body": "x",
                        "code_location": {
                            "absolute_file_path": "/tmp/a.py",
                            "line_range": "bad",
                        },
                        "confidence_score": 0.9,
                        "priority": 1,
                    }
                ],
                "overall_correctness": "incorrect",
                "overall_explanation": "x",
                "overall_confidence_score": 0.7,
            }
        )
        is None
    )
    assert (
        normalize_review_output(
            {
                "findings": [
                    {
                        "title": "bad",
                        "body": "x",
                        "code_location": {
                            "absolute_file_path": "/tmp/a.py",
                            "line_range": {"start": 0, "end": 1},
                        },
                        "confidence_score": 0.9,
                        "priority": 1,
                    }
                ],
                "overall_correctness": "incorrect",
                "overall_explanation": "x",
                "overall_confidence_score": 0.7,
            }
        )
        is None
    )
    assert ai_review_exhausted_summary([], 2).endswith(
        "Last accepted finding: review still reported blocking issues"
    )


@pytest.mark.asyncio
async def test_app_server_client_review_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, overrides={"codex": {"fast_mode": True}})
    client = AppServerClient(settings.coding_agent, settings.workspace)
    stops: list[str] = []
    started_dynamic_tools: list[list[dict[str, Any]] | None] = []

    class FakeSession:
        def __init__(self, name: str) -> None:
            self.name = name

        async def stop(self) -> None:
            stops.append(self.name)

    async def fake_start_session(
        _workspace: str,
        *,
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> FakeSession:
        started_dynamic_tools.append(dynamic_tools)
        return FakeSession("success")

    async def fake_run_session_review(*_args: Any, **_kwargs: Any) -> ReviewOutput:
        return _review_output()

    monkeypatch.setattr(client, "start_session", fake_start_session)
    monkeypatch.setattr(client, "run_session_review", fake_run_session_review)
    review_output = await client.run_review(
        "/tmp/workspace",
        "Review",
        make_issue(identifier="ENG-1"),
    )
    assert review_output.findings[0].title == "Finding"
    assert stops == ["success"]
    assert started_dynamic_tools == [[]]

    async def fake_start_session_error(
        _workspace: str,
        *,
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> FakeSession:
        started_dynamic_tools.append(dynamic_tools)
        return FakeSession("error")

    async def fail_run_session_review(*_args: Any, **_kwargs: Any) -> ReviewOutput:
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "start_session", fake_start_session_error)
    monkeypatch.setattr(client, "run_session_review", fail_run_session_review)
    with pytest.raises(RuntimeError, match="boom"):
        await client.run_review(
            "/tmp/workspace", "Review", make_issue(identifier="ENG-1")
        )
    assert stops == ["success", "error"]
    assert started_dynamic_tools == [[], []]


@pytest.mark.asyncio
async def test_run_session_review_emits_error_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    client = AppServerClient(settings.coding_agent, settings.workspace)
    session = make_session()
    messages: list[dict[str, Any]] = []
    started_turns: list[dict[str, Any]] = []

    async def collect(message: dict[str, Any]) -> None:
        messages.append(message)

    async def fake_start_turn(
        _session: Any,
        prompt: str,
        issue: Any,
        *,
        output_schema: dict[str, Any] | None = None,
        sandbox_policy: dict[str, Any] | None = None,
    ) -> str:
        started_turns.append(
            {
                "prompt": prompt,
                "issue": issue.identifier,
                "output_schema": output_schema,
                "sandbox_policy": sandbox_policy,
            }
        )
        return "turn-review-1"

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.start_turn",
        fake_start_turn,
    )
    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.await_review_completion",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=_review_output()),
    )
    review_output = await client.run_session_review(
        session,
        "Review",
        make_issue(identifier="ENG-1"),
        on_message=collect,
    )
    assert review_output.findings[0].title == "Finding"
    assert started_turns == [
        {
            "prompt": "Review",
            "issue": "ENG-1",
            "output_schema": review_output_schema(),
            "sandbox_policy": review_turn_sandbox_policy(
                settings.workspace.root,
                session.workspace,
            ),
        }
    ]
    assert any(message["event"] == "review_started" for message in messages)
    assert any(message["review_thread_id"] == session.thread_id for message in messages)

    async def fail_review(*_args: Any, **_kwargs: Any) -> ReviewOutput:
        raise AppServerError("review failed")

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.client.await_review_completion",
        fail_review,
    )
    with pytest.raises(AppServerError, match="review failed"):
        await client.run_session_review(
            session,
            "Review",
            make_issue(identifier="ENG-1"),
            on_message=collect,
        )
    assert any(message["event"] == "review_ended_with_error" for message in messages)


@pytest.mark.asyncio
async def test_codex_runtime_run_review_uses_override_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    calls: list[tuple[str | None, str | None, bool | None]] = []

    async def fake_run_review(
        self,
        workspace: str,
        prompt: str,
        issue: Any,
        *,
        on_message=None,
        tool_executor=None,
    ):  # type: ignore[no-untyped-def]
        calls.append(
            (
                self._coding_agent.model,
                self._coding_agent.reasoning_effort,
                self._coding_agent.fast_mode,
            )
        )
        return _review_output(title=prompt)

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.runtime.AppServerClient.run_review",
        fake_run_review,
    )
    from code_factory.coding_agents.codex.runtime import CodexRuntime

    codex_runtime = CodexRuntime(settings)
    default_output = await codex_runtime.run_review(
        "/tmp/workspace",
        "default",
        make_issue(identifier="ENG-1"),
    )
    override_output = await codex_runtime.run_review(
        "/tmp/workspace",
        "override",
        make_issue(identifier="ENG-1"),
        model="gpt-5.4-mini",
        reasoning_effort="high",
        fast_mode=False,
    )
    assert default_output.findings[0].title == "default"
    assert override_output.findings[0].title == "override"
    assert calls[0] == (
        settings.coding_agent.model,
        settings.coding_agent.reasoning_effort,
        settings.coding_agent.fast_mode,
    )
    assert calls[1] == ("gpt-5.4-mini", "high", False)


@pytest.mark.asyncio
async def test_await_review_completion_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect(message: dict[str, Any]) -> None:
        messages.append(message)

    messages: list[dict[str, Any]] = []
    session = make_session()
    await session.event_queue.put(("exit", 9))
    with pytest.raises(AppServerError, match="port_exit"):
        await await_review_completion(session, collect, cast(Any, object()))

    session = make_session()
    valid_item = {
        "method": "item/completed",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-review-1",
            "item": {
                "id": "message-1",
                "type": "agentMessage",
                "text": json.dumps(
                    {
                        "findings": [],
                        "overall_correctness": "patch is correct",
                        "overall_explanation": "Looks good.",
                        "overall_confidence_score": 0.8,
                    }
                ),
            },
        },
    }
    await session.event_queue.put(("stderr", "ignored"))
    await session.event_queue.put(("line", "not json"))
    await session.event_queue.put(
        (
            "line",
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "message-0",
                            "type": "agentMessage",
                            "text": "not json",
                        }
                    },
                }
            ),
        )
    )
    await session.event_queue.put(("line", json.dumps(valid_item)))
    await session.event_queue.put(
        (
            "line",
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                }
            ),
        )
    )
    output = await await_review_completion(session, collect, cast(Any, object()))
    assert output.overall_correctness == "patch is correct"
    assert any(message["event"] == "malformed" for message in messages)
    for payload, expected in [
        (
            {"method": "turn/completed", "params": {"turn": {"status": "failed"}}},
            "turn_failed",
        ),
        (
            {
                "method": "turn/completed",
                "params": {"turn": {"status": "interrupted"}},
            },
            "turn_cancelled",
        ),
        (
            {"method": "turn/completed", "params": {"turn": {"status": "weird"}}},
            "invalid_turn_status",
        ),
        (
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
            "missing_review_output",
        ),
        (
            {"method": "turn/failed", "params": {"reason": "boom"}},
            "turn_failed",
        ),
        (
            {"method": "turn/cancelled", "params": {"reason": "boom"}},
            "turn_cancelled",
        ),
    ]:
        session = make_session()
        await session.event_queue.put(("line", json.dumps(payload)))
        with pytest.raises(AppServerError, match=expected):
            await await_review_completion(session, collect, cast(Any, object()))

    session = make_session()
    handled: list[str] = []

    async def fake_handle_turn_message(
        _session: Any,
        _on_message: Any,
        _tool_executor: Any,
        message: dict[str, Any],
        _raw: str,
    ) -> str:
        handled.append("handled")
        if message.get("method") == "turn/completed":
            return "turn_completed"
        return "continue"

    monkeypatch.setattr(
        "code_factory.coding_agents.codex.app_server.reviews.handle_turn_message",
        fake_handle_turn_message,
    )
    session = make_session()
    await session.event_queue.put(
        ("line", json.dumps({"method": "item/tool/call", "params": {}}))
    )
    await session.event_queue.put(("line", json.dumps(valid_item)))
    await session.event_queue.put(
        (
            "line",
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                }
            ),
        )
    )
    await await_review_completion(session, collect, cast(Any, object()))
    assert handled == ["handled", "handled", "handled"]

    session = make_session()
    await session.event_queue.put(("line", json.dumps({"unexpected": True})))
    await session.event_queue.put(("line", json.dumps(valid_item)))
    await session.event_queue.put(
        (
            "line",
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                }
            ),
        )
    )
    await await_review_completion(session, collect, cast(Any, object()))
    assert handled[-3:] == ["handled", "handled", "handled"]


def test_extract_review_output_reads_only_normal_agent_messages() -> None:
    message = {
        "method": "item/completed",
        "params": {
            "item": {
                "id": "message-1",
                "type": "agentMessage",
                "text": json.dumps(
                    {
                        "findings": [],
                        "overall_correctness": "patch is correct",
                        "overall_explanation": "ok",
                        "overall_confidence_score": 0.7,
                    }
                ),
            }
        },
    }
    assert extract_review_output(message) is not None
    assert (
        extract_review_output(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "message-2",
                        "type": "agentMessage",
                        "text": "not-json",
                    }
                },
            }
        )
        is None
    )
    assert (
        extract_review_output(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "message-3",
                        "type": "agentMessage",
                        "text": 7,
                    }
                },
            }
        )
        is None
    )
    assert extract_review_output({"params": {"review_output": {}}}) is None


@pytest.mark.asyncio
async def test_disabled_dynamic_tool_executor_reports_unsupported_tool_call() -> None:
    outcome = await DisabledDynamicToolExecutor().execute("tracker_issue_get", {})

    assert outcome.event == "unsupported_tool_call"
    assert outcome.success is False
    assert outcome.payload["error"]["message"] == (
        "Dynamic tools are disabled for review turns."
    )


@pytest.mark.asyncio
async def test_ai_review_gate_and_completion_block_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "ai_review": "Security",
                    "hooks": {
                        "before_complete": "check",
                        "before_complete_max_feedback_loops": 0,
                    },
                },
            },
            ai_review={"types": {"Security": {"prompt": "security"}}},
            prompt="# prompt: default\nImplement.\n\n# review: security\nReview.\n",
        )
    )
    profile = snapshot.state_profile("In Progress")
    assert profile is not None

    async def fake_review_pass(*_args: Any, **_kwargs: Any) -> AiReviewPassResult:
        review_output = _review_output()
        return AiReviewPassResult(
            selection=WorktreeReviewSelection(
                surface=WorktreeReviewSurface(
                    changed_paths=("src/app.py",),
                    lines_changed=5,
                ),
                matched_types=snapshot.ai_review_types_for_state("In Progress"),
            ),
            executed_reviews=(
                ExecutedAiReview(
                    review_type=snapshot.ai_review_types_for_state("In Progress")[0],
                    review_output=review_output,
                    accepted_findings=review_output.findings,
                ),
            ),
        )

    monkeypatch.setattr(
        "code_factory.runtime.worker.ai_review.run_ai_review_pass",
        fake_review_pass,
    )

    blocked = await run_ai_review_gate(
        runtime=cast(Any, object()),
        workflow_snapshot=snapshot,
        workspace_path="/tmp/workspace",
        issue=make_issue(identifier="ENG-1"),
        profile=profile,
        queue=asyncio.Queue(),
        issue_id="issue-1",
        feedback_attempts=0,
        failure_state="Failed",
        on_message=None,
    )
    assert blocked is not None
    assert blocked[2] is not None
    assert blocked[2].next_state == "Failed"

    skipped = await run_ai_review_gate(
        runtime=cast(Any, object()),
        workflow_snapshot=snapshot,
        workspace_path="/tmp/workspace",
        issue=make_issue(identifier="ENG-1"),
        profile=profile,
        queue=asyncio.Queue(),
        issue_id=None,
        feedback_attempts=0,
        failure_state="Failed",
        on_message=None,
    )
    assert skipped is not None

    async def fake_no_match_review_pass(
        *_args: Any, **_kwargs: Any
    ) -> AiReviewPassResult:
        return AiReviewPassResult(
            selection=WorktreeReviewSelection(
                surface=WorktreeReviewSurface(
                    changed_paths=("src/app.py",),
                    lines_changed=5,
                ),
                matched_types=(),
            ),
            executed_reviews=(),
        )

    monkeypatch.setattr(
        "code_factory.runtime.worker.ai_review.run_ai_review_pass",
        fake_no_match_review_pass,
    )
    assert (
        await run_ai_review_gate(
            runtime=cast(Any, object()),
            workflow_snapshot=snapshot,
            workspace_path="/tmp/workspace",
            issue=make_issue(identifier="ENG-1"),
            profile=profile,
            queue=asyncio.Queue(),
            issue_id=None,
            feedback_attempts=0,
            failure_state="Failed",
            on_message=None,
        )
        is None
    )

    async def fake_run_turn(_prompt: str) -> StructuredTurnResult:
        return StructuredTurnResult(
            decision="transition", summary="done", next_state="Done"
        )

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.native_readiness_result",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_before_complete_hook",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(status=0, stdout="ok", stderr=""),
        ),
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_ai_review_gate",
        lambda **_kwargs: asyncio.sleep(
            0,
            result=(
                1,
                "",
                StructuredTurnResult(
                    decision="blocked",
                    summary="blocked",
                    next_state="Failed",
                ),
            ),
        ),
    )
    result = await run_pre_complete_turns(
        run_turn=fake_run_turn,
        settings=snapshot.settings,
        workspace_path="/tmp/workspace",
        issue=make_issue(identifier="ENG-1"),
        profile=profile,
        queue=asyncio.Queue(),
        issue_id="issue-1",
        failure_state="Failed",
        initial_prompt="prompt",
        should_stop=lambda: False,
        workflow_snapshot=snapshot,
        runtime=cast(Any, object()),
    )
    assert result.next_state == "Failed"


@pytest.mark.asyncio
async def test_run_pre_complete_turns_runs_deterministic_gates_before_ai_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "ai_review": "Security",
                    "hooks": {"before_complete": "check"},
                },
            },
            ai_review={"types": {"Security": {"prompt": "security"}}},
            prompt="# prompt: default\nImplement.\n\n# review: security\nReview.\n",
        )
    )
    profile = snapshot.state_profile("In Progress")
    assert profile is not None
    call_order: list[str] = []

    async def fake_run_turn(_prompt: str) -> StructuredTurnResult:
        call_order.append("turn")
        return StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        )

    async def fake_native(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("native")
        return None

    async def fake_hook(*_args: Any, **_kwargs: Any) -> Any:
        call_order.append("hook")
        return SimpleNamespace(status=0, stdout="ok", stderr="")

    async def fake_ai_review_gate(**_kwargs: Any) -> None:
        call_order.append("ai_review")
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.native_readiness_result",
        fake_native,
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_before_complete_hook",
        fake_hook,
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_ai_review_gate",
        fake_ai_review_gate,
    )

    result = await run_pre_complete_turns(
        run_turn=fake_run_turn,
        settings=snapshot.settings,
        workspace_path="/tmp/workspace",
        issue=make_issue(identifier="ENG-1"),
        profile=profile,
        queue=asyncio.Queue(),
        issue_id="issue-1",
        failure_state="Failed",
        initial_prompt="prompt",
        should_stop=lambda: False,
        workflow_snapshot=snapshot,
        runtime=cast(Any, object()),
    )

    assert result.next_state == "Done"
    assert call_order == ["turn", "native", "hook", "ai_review"]


@pytest.mark.asyncio
async def test_run_pre_complete_turns_runs_gates_on_final_allowed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "ai_review": "Security",
                    "completion": {"require_pushed_head": True},
                    "hooks": {
                        "before_complete": "check",
                        "before_complete_max_feedback_loops": 1,
                    },
                },
            },
            ai_review={"types": {"Security": {"prompt": "security"}}},
            prompt="# prompt: default\nImplement.\n\n# review: security\nReview.\n",
        )
    )
    profile = snapshot.state_profile("In Progress")
    assert profile is not None
    prompts: list[str] = []
    call_order: list[str] = []
    native_statuses = iter(
        (
            SimpleNamespace(status=2, stdout="", stderr="still not pushed"),
            None,
        )
    )

    async def fake_run_turn(prompt: str) -> StructuredTurnResult:
        prompts.append(prompt)
        return StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        )

    async def fake_native(*_args: Any, **_kwargs: Any) -> Any:
        call_order.append("native")
        return next(native_statuses)

    async def fake_hook(*_args: Any, **_kwargs: Any) -> Any:
        call_order.append("hook")
        return SimpleNamespace(status=0, stdout="ok", stderr="")

    async def fake_ai_review_gate(**_kwargs: Any) -> None:
        call_order.append("ai_review")
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.native_readiness_result",
        fake_native,
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_before_complete_hook",
        fake_hook,
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_ai_review_gate",
        fake_ai_review_gate,
    )

    result = await run_pre_complete_turns(
        run_turn=fake_run_turn,
        settings=snapshot.settings,
        workspace_path="/tmp/workspace",
        issue=make_issue(identifier="ENG-1", branch_name="codex/eng-1"),
        profile=profile,
        queue=asyncio.Queue(),
        issue_id="issue-1",
        failure_state="Failed",
        initial_prompt="prompt",
        should_stop=lambda: False,
        workflow_snapshot=snapshot,
        runtime=cast(Any, object()),
    )

    assert result.decision == "transition"
    assert result.next_state == "Done"
    assert len(prompts) == 2
    assert prompts[0] == "prompt"
    assert "Feedback attempt 1 of 1." in prompts[1]
    assert call_order == ["native", "native", "hook", "ai_review"]


@pytest.mark.asyncio
async def test_run_pre_complete_turns_blocks_on_native_gate_without_feedback_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "completion": {"require_pushed_head": True},
                    "hooks": {"before_complete_max_feedback_loops": 0},
                },
            },
        )
    )
    profile = snapshot.state_profile("In Progress")
    assert profile is not None
    call_order: list[str] = []

    async def fake_run_turn(_prompt: str) -> StructuredTurnResult:
        return StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        )

    async def fake_native(*_args: Any, **_kwargs: Any) -> Any:
        call_order.append("native")
        return SimpleNamespace(status=2, stdout="", stderr="")

    async def fake_ai_review_gate(**_kwargs: Any) -> None:
        call_order.append("ai_review")
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.native_readiness_result",
        fake_native,
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_ai_review_gate",
        fake_ai_review_gate,
    )

    result = await run_pre_complete_turns(
        run_turn=fake_run_turn,
        settings=snapshot.settings,
        workspace_path="/tmp/workspace",
        issue=make_issue(identifier="ENG-1", branch_name="codex/eng-1"),
        profile=profile,
        queue=asyncio.Queue(),
        issue_id="issue-1",
        failure_state="Failed",
        initial_prompt="prompt",
        should_stop=lambda: False,
        workflow_snapshot=snapshot,
        runtime=cast(Any, object()),
    )

    assert result.decision == "blocked"
    assert result.next_state == "Failed"
    assert (
        result.summary
        == "Code Factory exhausted before_complete repair loops after 0 attempt(s). "
        "Last error: unknown failure"
    )
    assert call_order == ["native"]


@pytest.mark.asyncio
async def test_run_pre_complete_turns_blocks_on_hook_without_feedback_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Done"],
                    "hooks": {
                        "before_complete": "check",
                        "before_complete_max_feedback_loops": 0,
                    },
                },
            },
        )
    )
    profile = snapshot.state_profile("In Progress")
    assert profile is not None
    call_order: list[str] = []

    async def fake_run_turn(_prompt: str) -> StructuredTurnResult:
        return StructuredTurnResult(
            decision="transition",
            summary="done",
            next_state="Done",
        )

    async def fake_native(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("native")
        return None

    async def fake_hook(*_args: Any, **_kwargs: Any) -> Any:
        call_order.append("hook")
        return SimpleNamespace(status=2, stdout="", stderr="fix lint")

    async def fake_ai_review_gate(**_kwargs: Any) -> None:
        call_order.append("ai_review")
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.native_readiness_result",
        fake_native,
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_before_complete_hook",
        fake_hook,
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.completion.run_ai_review_gate",
        fake_ai_review_gate,
    )

    result = await run_pre_complete_turns(
        run_turn=fake_run_turn,
        settings=snapshot.settings,
        workspace_path="/tmp/workspace",
        issue=make_issue(identifier="ENG-1"),
        profile=profile,
        queue=asyncio.Queue(),
        issue_id="issue-1",
        failure_state="Failed",
        initial_prompt="prompt",
        should_stop=lambda: False,
        workflow_snapshot=snapshot,
        runtime=cast(Any, object()),
    )

    assert result.decision == "blocked"
    assert result.next_state == "Failed"
    assert (
        result.summary
        == "Code Factory exhausted before_complete repair loops after 0 attempt(s). "
        "Last error: fix lint"
    )
    assert call_order == ["native", "hook"]
