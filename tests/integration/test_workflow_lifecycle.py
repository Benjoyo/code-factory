from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from code_factory.issues import BlockerRef
from code_factory.structured_results import parse_result_comment

from ..conftest import make_issue, write_workflow_file
from .helpers import hook_script, issue_state, read_lines, wait_for_snapshot
from .support import IntegrationHarness, TurnPlan, transition_result


@pytest.mark.asyncio
async def test_integration_worker_driven_lifecycle_runs_hooks_and_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook_log = tmp_path / "hooks.log"
    issue = make_issue(id="issue-101", identifier="ENG-101", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "states": {
                "Todo": {"prompt": "default"},
                "Review": {"prompt": "default"},
                "Merging": {"prompt": "default"},
            },
            "hooks": {
                "after_create": hook_script(hook_log, "after_create"),
                "before_run": hook_script(hook_log, "before_run"),
                "after_run": hook_script(hook_log, "after_run", exit_status=8),
                "before_remove": hook_script(hook_log, "before_remove", exit_status=9),
            },
        },
        plans_by_identifier={
            "ENG-101": [
                TurnPlan(
                    result=transition_result("Review", summary="reviewed"),
                    token_usage={
                        "inputTokens": 10,
                        "outputTokens": 4,
                        "totalTokens": 14,
                    },
                ),
                TurnPlan(
                    result=transition_result("Merging", summary="ready to merge"),
                    token_usage={
                        "inputTokens": 13,
                        "outputTokens": 6,
                        "totalTokens": 19,
                    },
                ),
                TurnPlan(
                    result=transition_result("Done", summary="shipped"),
                    token_usage={
                        "inputTokens": 14,
                        "outputTokens": 7,
                        "totalTokens": 21,
                    },
                ),
            ]
        },
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: issue_state(harness, "issue-101") == "Done")
        workspace = tmp_path / "workspaces" / "ENG-101"
        await harness.wait_until(lambda: not workspace.exists())
        await harness.wait_until(lambda: read_lines(hook_log))

        created_comments = [
            event[2] for event in harness.tracker.events if event[0] == "create_comment"
        ]
        parsed_comments = [parse_result_comment(body) for body in created_comments]
        assert all(parsed is not None for parsed in parsed_comments)
        assert [parsed[0] for parsed in parsed_comments if parsed is not None] == [
            "Todo",
            "Review",
            "Merging",
        ]
        assert [
            parsed[1].summary for parsed in parsed_comments if parsed is not None
        ] == [
            "reviewed",
            "ready to merge",
            "shipped",
        ]
        assert read_lines(hook_log) == [
            "ENG-101:after_create",
            "ENG-101:before_run",
            "ENG-101:after_run",
            "ENG-101:before_run",
            "ENG-101:after_run",
            "ENG-101:before_run",
            "ENG-101:after_run",
            "ENG-101:before_remove",
        ]
        prompts = harness.controller.prompt_log["ENG-101"]
        assert len(prompts) == 3
        assert all(
            "You are an agent for this repository." in prompt for prompt in prompts
        )
        snapshot = await harness.snapshot()
        assert snapshot["running"] == []
        assert snapshot["retrying"] == []
        assert snapshot["agent_totals"]["total_tokens"] == 54


@pytest.mark.asyncio
async def test_integration_review_to_rework_spans_multiple_worker_lifetimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook_log = tmp_path / "hooks.log"
    issue = make_issue(id="issue-202", identifier="ENG-202", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "states": {
                "Todo": {"prompt": "default"},
                "Review": {"prompt": "default"},
                "Rework": {"prompt": "default"},
            },
            "hooks": {
                "after_create": hook_script(hook_log, "after_create"),
                "before_run": hook_script(hook_log, "before_run"),
                "after_run": hook_script(hook_log, "after_run"),
                "before_remove": hook_script(hook_log, "before_remove"),
            },
        },
        plans_by_identifier={
            "ENG-202": [
                TurnPlan(result=transition_result("Review", summary="review ready")),
                TurnPlan(
                    result=transition_result("Rework", summary="changes requested")
                ),
                TurnPlan(result=transition_result("Done", summary="reworked")),
            ]
        },
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: issue_state(harness, "issue-202") == "Done")
        workspace = str(tmp_path / "workspaces" / "ENG-202")
        await harness.wait_until(lambda: not Path(workspace).exists())

        assert harness.controller.started_workspaces == [
            workspace,
            workspace,
            workspace,
        ]
        assert read_lines(hook_log) == [
            "ENG-202:after_create",
            "ENG-202:before_run",
            "ENG-202:after_run",
            "ENG-202:before_run",
            "ENG-202:after_run",
            "ENG-202:before_run",
            "ENG-202:after_run",
            "ENG-202:before_remove",
        ]
        prompts = harness.controller.prompt_log["ENG-202"]
        assert len(prompts) == 3
        assert all(
            "You are an agent for this repository." in prompt for prompt in prompts
        )


@pytest.mark.asyncio
async def test_integration_state_transitions_start_fresh_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = make_issue(id="issue-203", identifier="ENG-203", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={
            "prompt": (
                "# prompt: default\n"
                "Execution prompt for {{ issue.state }}.\n\n"
                "# prompt: merge\n"
                "Merge prompt for {{ issue.state }}.\n"
            ),
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "states": {
                "Todo": {"prompt": "default"},
                "In Progress": {"prompt": "default"},
                "Merging": {
                    "prompt": "merge",
                    "codex": {"model": "gpt-5.4-mini", "reasoning_effort": "low"},
                },
            },
        },
        plans_by_identifier={
            "ENG-203": [
                TurnPlan(result=transition_result("In Progress", summary="started")),
                TurnPlan(result=transition_result("Merging", summary="ready to merge")),
                TurnPlan(result=transition_result("Done", summary="merged")),
            ]
        },
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: issue_state(harness, "issue-203") == "Done")

        prompts = harness.controller.prompt_log["ENG-203"]
        workspace = str(tmp_path / "workspaces" / "ENG-203")
        assert len(prompts) == 3
        assert "Execution prompt for Todo." in prompts[0]
        assert "Execution prompt for In Progress." in prompts[1]
        assert "Merge prompt for Merging." in prompts[2]
        assert harness.controller.started_workspaces == [
            workspace,
            workspace,
            workspace,
        ]


@pytest.mark.asyncio
async def test_integration_todo_blocked_by_non_terminal_issue_waits_until_unblocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = make_issue(id="issue-301", identifier="ENG-301", state="Todo")
    blocked = make_issue(
        id="issue-302",
        identifier="ENG-302",
        state="Todo",
        blocked_by=(BlockerRef(id="issue-301", identifier="ENG-301", state="Todo"),),
    )

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[blocker, blocked],
        workflow_overrides={"tracker": {"terminal_states": ["Done", "Canceled"]}},
        plans_by_identifier={
            "ENG-301": [
                TurnPlan(
                    sleep_ms=80,
                    result=transition_result("Done", summary="unblocked"),
                )
            ],
            "ENG-302": [
                TurnPlan(
                    result=transition_result("Done", summary="completed dependent work")
                )
            ],
        },
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-301" in harness.controller.prompt_log)
        assert "ENG-302" not in harness.controller.prompt_log

        await harness.wait_until(lambda: issue_state(harness, "issue-301") == "Done")
        await harness.wait_until(lambda: "ENG-302" in harness.controller.prompt_log)
        await harness.wait_until(lambda: issue_state(harness, "issue-302") == "Done")

        assert [
            Path(path).name for path in harness.controller.started_workspaces[:2]
        ] == [
            "ENG-301",
            "ENG-302",
        ]


@pytest.mark.asyncio
async def test_integration_upstream_results_are_available_in_prompt_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = make_issue(id="issue-321", identifier="ENG-321", state="Build")
    blocked = make_issue(
        id="issue-322",
        identifier="ENG-322",
        state="Build",
        blocked_by=(BlockerRef(id="issue-321", identifier="ENG-321", state="Build"),),
    )

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[blocker, blocked],
        workflow_overrides={
            "prompt": (
                "# prompt: default\n"
                "{% if issue.upstream_tickets.size > 0 %}"
                "Upstream: {{ issue.upstream_tickets[0].results_by_state.Build.summary }}\n"
                "{% endif %}"
                "State: {{ issue.state }}\n"
            ),
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "states": {"Build": {"prompt": "default"}},
        },
        plans_by_identifier={
            "ENG-321": [
                TurnPlan(result=transition_result("Done", summary="artifact ready"))
            ],
            "ENG-322": [
                TurnPlan(result=transition_result("Done", summary="consumed artifact"))
            ],
        },
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: issue_state(harness, "issue-321") == "Done")
        await harness.wait_until(lambda: issue_state(harness, "issue-322") == "Done")

        prompts = harness.controller.prompt_log["ENG-322"]
        assert len(prompts) == 1
        assert "Upstream: artifact ready" in prompts[0]


@pytest.mark.asyncio
async def test_integration_global_and_state_concurrency_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = [
        make_issue(id="issue-401", identifier="ENG-401", state="Todo"),
        make_issue(id="issue-402", identifier="ENG-402", state="Todo"),
        make_issue(id="issue-403", identifier="ENG-403", state="Review"),
    ]

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=issues,
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "states": {
                "Todo": {"prompt": "default"},
                "Review": {"prompt": "default"},
            },
            "agent": {
                "max_concurrent_agents": 2,
                "max_concurrent_agents_by_state": {"Todo": 1},
            },
        },
        plans_by_identifier={
            "ENG-401": [TurnPlan(sleep_ms=120, result=transition_result("Done"))],
            "ENG-402": [TurnPlan(sleep_ms=120, result=transition_result("Done"))],
            "ENG-403": [TurnPlan(sleep_ms=120, result=transition_result("Done"))],
        },
    ) as harness:
        await harness.refresh()
        snapshot = await wait_for_snapshot(
            harness, lambda current: len(current["running"]) == 2
        )
        running_states = sorted(entry["state"] for entry in snapshot["running"])
        assert running_states == ["Review", "Todo"]
        assert "ENG-402" not in harness.controller.prompt_log

        await harness.wait_until(lambda: "ENG-402" in harness.controller.prompt_log)
        await harness.wait_until(
            lambda: all(
                issue_state(harness, issue.id or "") == "Done" for issue in issues
            )
        )


@pytest.mark.asyncio
async def test_integration_non_terminal_reassignment_and_terminal_cancel_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel_issue = make_issue(id="issue-611", identifier="ENG-611", state="In Progress")
    reassign_issue = make_issue(
        id="issue-612", identifier="ENG-612", state="In Progress"
    )

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[cancel_issue],
        workflow_overrides={"tracker": {"terminal_states": ["Done", "Canceled"]}},
        plans_by_identifier={
            "ENG-611": [TurnPlan(pause_until_stopped=True)],
            "ENG-612": [TurnPlan(pause_until_stopped=True)],
        },
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-611" in harness.controller.prompt_log)
        cancel_workspace = tmp_path / "workspaces" / "ENG-611"
        harness.tracker.mutate_issue("issue-611", state="Canceled")
        await harness.refresh()
        await harness.wait_until(lambda: not cancel_workspace.exists())
        assert not (await harness.snapshot())["running"]

        harness.tracker.upsert_issue(reassign_issue)
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-612" in harness.controller.prompt_log)
        reassign_workspace = tmp_path / "workspaces" / "ENG-612"
        harness.tracker.mutate_issue("issue-612", assigned_to_worker=False)
        await harness.refresh()
        await wait_for_snapshot(harness, lambda snapshot: not snapshot["running"])
        assert reassign_workspace.exists()


@pytest.mark.asyncio
async def test_integration_before_run_timeout_aborts_attempt_and_still_runs_after_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook_log = tmp_path / "hooks.log"
    issue = make_issue(id="issue-701", identifier="ENG-701", state="Todo")

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue],
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "hooks": {
                "before_run": "sleep 0.2",
                "after_run": hook_script(hook_log, "after_run"),
                "timeout_ms": 30,
            },
            "agent": {"max_retry_backoff_ms": 60},
        },
        plans_by_identifier={"ENG-701": [TurnPlan(result=transition_result("Done"))]},
    ) as harness:
        await harness.refresh()
        retry_entry = await wait_for_snapshot(
            harness,
            lambda snapshot: next(
                (
                    entry
                    for entry in snapshot["retrying"]
                    if entry["issue_id"] == "issue-701"
                ),
                None,
            ),
        )
        assert "workspace_hook_timeout" in retry_entry["error"]
        assert "ENG-701" not in harness.controller.prompt_log
        assert read_lines(hook_log) == ["ENG-701:after_run"]

        harness.tracker.mutate_issue("issue-701", state="Canceled")
        await harness.refresh()
        await wait_for_snapshot(harness, lambda snapshot: not snapshot["retrying"])


@pytest.mark.asyncio
async def test_integration_workflow_reload_applies_new_config_and_invalid_reload_keeps_last_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hook_log = tmp_path / "hooks.log"
    issue_one = make_issue(id="issue-801", identifier="ENG-801", state="Todo")
    issue_two = make_issue(id="issue-802", identifier="ENG-802", state="Review")
    issue_three = make_issue(id="issue-803", identifier="ENG-803", state="Review")

    def rewrite(before_run_label: str, active_states: list[str]) -> None:
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            tracker={"kind": "memory", "terminal_states": ["Done", "Canceled"]},
            states={state: {"prompt": "default"} for state in active_states},
            polling={"interval_ms": 25},
            workspace={"root": str(tmp_path / "workspaces")},
            codex={"command": "dummy-agent"},
            hooks={"before_run": hook_script(hook_log, before_run_label)},
        )

    async with IntegrationHarness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        issues=[issue_one, issue_two],
        run_workflow_store=True,
        workflow_overrides={
            "tracker": {"terminal_states": ["Done", "Canceled"]},
            "states": {"Todo": {"prompt": "default"}},
            "hooks": {"before_run": hook_script(hook_log, "v1")},
        },
        plans_by_identifier={
            "ENG-801": [TurnPlan(result=transition_result("Done"))],
            "ENG-802": [TurnPlan(result=transition_result("Done"))],
            "ENG-803": [TurnPlan(result=transition_result("Done"))],
        },
    ) as harness:
        await harness.refresh()
        await harness.wait_until(lambda: issue_state(harness, "issue-801") == "Done")
        await asyncio.sleep(0.1)
        assert "ENG-802" not in harness.controller.prompt_log

        rewrite("v2", ["Todo", "Review"])
        await harness.wait_until(
            lambda: harness.actor and harness.actor.workflow_snapshot.version >= 2
        )
        await harness.wait_until(lambda: "ENG-802" in harness.controller.prompt_log)

        with caplog.at_level(logging.ERROR):
            (tmp_path / "WORKFLOW.md").write_text("---\n[invalid\n", encoding="utf-8")
            await harness.wait_until(
                lambda: harness.actor
                and harness.actor.workflow_reload_error is not None
            )

        harness.tracker.upsert_issue(issue_three)
        await harness.refresh()
        await harness.wait_until(lambda: "ENG-803" in harness.controller.prompt_log)

        assert read_lines(hook_log) == [
            "ENG-801:v1",
            "ENG-802:v2",
            "ENG-803:v2",
        ]
        assert any(
            "keeping last known good configuration" in record.message
            for record in caplog.records
        )
