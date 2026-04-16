from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import code_factory.runtime.orchestration.native_merge as native_merge_module
import code_factory.runtime.orchestration.native_merge_feedback as feedback_module
from code_factory.runtime.orchestration import OrchestratorActor
from code_factory.runtime.orchestration.native_merge import attempt_native_merge
from code_factory.runtime.worker.actor import IssueWorker
from code_factory.structured_results import parse_result_comment
from code_factory.trackers.memory import MemoryTracker
from code_factory.workspace.review.review_resolution import ReviewError
from code_factory.workspace.review.review_shell import ShellResult

from ..conftest import make_issue, make_snapshot, write_workflow_file


@pytest.fixture(autouse=True)
def patch_issue_worker_workpad(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.hydrate_workspace_workpad", _noop
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.sync_workspace_workpad", _noop
    )
    monkeypatch.setattr(
        "code_factory.runtime.worker.actor.prepare_workspace_repository", _noop
    )


@pytest.fixture(autouse=True)
def patch_github_cli_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "code_factory.workspace.review.review_resolution.shutil.which",
        lambda command: "/usr/bin/gh" if command == "gh" else None,
    )


def _merging_workflow(tmp_path: Path) -> Path:
    return write_workflow_file(
        tmp_path / "WORKFLOW.md",
        states={
            "Todo": {"auto_next_state": "In Progress"},
            "Merging": {
                "prompt": "default",
                "allowed_next_states": ["Done", "Rework"],
                "merge": {"mode": "native_then_agent"},
            },
        },
    )


def _shell_capture_factory(responses: list[tuple[str, ShellResult]], calls: list[str]):
    async def _capture(command: str, *, cwd: str, env: dict[str, str] | None = None):
        del cwd, env
        calls.append(command)
        for pattern, result in responses:
            if pattern in command:
                return result
        raise AssertionError(f"unexpected command: {command}")

    return _capture


def _json_result(payload: Any) -> ShellResult:
    return ShellResult(status=0, stdout=json.dumps(payload), stderr="")


def _success_responses(
    *,
    issue_comments: list[dict[str, Any]] | None = None,
    review_comments: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    check_runs: list[dict[str, Any]] | None = None,
    pr_list: list[dict[str, Any]] | None = None,
    pr_view: dict[str, Any] | None = None,
    pr_head_sha: str = "abc123",
    pr_head_repo: str = "fork-owner/fork-repo",
    merge_result: ShellResult | None = None,
) -> list[tuple[str, ShellResult]]:
    return [
        ("git rev-parse --is-inside-work-tree", ShellResult(0, "true\n", "")),
        ("gh auth status", ShellResult(0, "", "")),
        (
            "gh pr list --head",
            _json_result(pr_list if pr_list is not None else [{"number": 12}]),
        ),
        (
            "gh pr view 12 --json",
            _json_result(
                pr_view
                if pr_view is not None
                else {
                    "number": 12,
                    "url": "https://example/pr/12",
                    "headRefOid": "abc123",
                    "headRefName": "codex/eng-1",
                    "title": "PR title",
                    "body": "PR body",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                }
            ),
        ),
        (
            "gh api repos/{owner}/{repo}/pulls/12",
            _json_result(
                {"head": {"sha": pr_head_sha, "repo": {"full_name": pr_head_repo}}}
            ),
        ),
        (
            f"gh api repos/{pr_head_repo}/commits/abc123/check-runs -f per_page=100 -f page=1",
            _json_result(
                {
                    "total_count": len(
                        check_runs
                        or [
                            {
                                "name": "ci",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    ),
                    "check_runs": check_runs
                    or [{"name": "ci", "status": "completed", "conclusion": "success"}],
                }
            ),
        ),
        (
            f"gh api repos/{pr_head_repo}/commits/abc123/check-runs -f per_page=100 -f page=2",
            _json_result(
                {
                    "total_count": len(
                        check_runs
                        or [
                            {
                                "name": "ci",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    ),
                    "check_runs": [],
                }
            ),
        ),
        (
            "gh api --method GET repos/{owner}/{repo}/issues/12/comments -f per_page=100 -f page=1",
            _json_result(issue_comments or []),
        ),
        (
            "gh api --method GET repos/{owner}/{repo}/issues/12/comments -f per_page=100 -f page=2",
            _json_result([]),
        ),
        (
            "gh api --method GET repos/{owner}/{repo}/pulls/12/comments -f per_page=100 -f page=1",
            _json_result(review_comments or []),
        ),
        (
            "gh api --method GET repos/{owner}/{repo}/pulls/12/comments -f per_page=100 -f page=2",
            _json_result([]),
        ),
        (
            "gh api --method GET repos/{owner}/{repo}/pulls/12/reviews -f per_page=100 -f page=1",
            _json_result(reviews or []),
        ),
        (
            "gh api --method GET repos/{owner}/{repo}/pulls/12/reviews -f per_page=100 -f page=2",
            _json_result([]),
        ),
        (
            "gh pr merge 12 --squash --delete-branch",
            merge_result or ShellResult(0, "merged\n", ""),
        ),
    ]


@pytest.mark.asyncio
async def test_attempt_native_merge_succeeds_and_persists_state_result(
    tmp_path: Path,
) -> None:
    workflow = _merging_workflow(tmp_path)
    snapshot = make_snapshot(workflow)
    issue = make_issue(
        id="issue-1",
        identifier="ENG-1",
        state="Merging",
        branch_name="codex/eng-1",
    )
    tracker = MemoryTracker([issue])
    calls: list[str] = []

    result = await attempt_native_merge(
        issue,
        snapshot,
        tracker,
        shell_capture=_shell_capture_factory(_success_responses(), calls),
    )

    assert result.merged is True
    assert any(
        "gh api repos/fork-owner/fork-repo/commits/abc123/check-runs" in call
        for call in calls
    )
    assert any("gh pr merge 12 --squash --delete-branch" in call for call in calls)
    assert (await tracker.fetch_issue_states_by_ids(["issue-1"]))[0].state == "Done"
    comments = await tracker.fetch_issue_comments("issue-1")
    parsed = parse_result_comment(comments[0].body if comments else None)
    assert parsed is not None
    assert parsed[0] == "Merging"
    assert parsed[1].next_state == "Done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "issue", "message"),
    [
        (
            [
                ("git rev-parse --is-inside-work-tree", ShellResult(0, "true\n", "")),
                ("gh auth status", ShellResult(1, "", "not logged in")),
            ],
            make_issue(id="issue-2", state="Merging", branch_name="codex/eng-2"),
            "not logged in",
        ),
        (
            _success_responses(
                pr_list=[{"number": 12}, {"number": 13}],
            ),
            make_issue(id="issue-3", state="Merging", branch_name="codex/eng-3"),
            "Multiple open PRs found",
        ),
        (
            _success_responses(
                pr_view={
                    "number": 12,
                    "url": "https://example/pr/12",
                    "headRefOid": "abc123",
                    "headRefName": "codex/eng-4",
                    "title": "PR title",
                    "body": "PR body",
                    "mergeable": "UNKNOWN",
                    "mergeStateStatus": "UNKNOWN",
                }
            ),
            make_issue(id="issue-4", state="Merging", branch_name="codex/eng-4"),
            "mergeability is UNKNOWN",
        ),
        (
            _success_responses(pr_head_sha="def456"),
            make_issue(id="issue-5", state="Merging", branch_name="codex/eng-5"),
            "latest branch head",
        ),
        (
            _success_responses(
                check_runs=[{"name": "ci", "status": "queued", "conclusion": None}]
            ),
            make_issue(id="issue-6", state="Merging", branch_name="codex/eng-6"),
            "checks are still pending",
        ),
        (
            _success_responses(
                check_runs=[
                    {"name": "ci", "status": "completed", "conclusion": "failure"}
                ]
            ),
            make_issue(id="issue-7", state="Merging", branch_name="codex/eng-7"),
            "checks are failing",
        ),
        (
            _success_responses(
                issue_comments=[
                    {
                        "id": 1,
                        "body": "please fix this",
                        "user": {"login": "alice", "type": "User"},
                        "created_at": "2024-01-01T00:00:00Z",
                        "updated_at": "2024-01-01T00:00:00Z",
                    }
                ]
            ),
            make_issue(id="issue-8", state="Merging", branch_name="codex/eng-8"),
            "unresolved review comments",
        ),
        (
            _success_responses(
                reviews=[
                    {
                        "id": 1,
                        "state": "CHANGES_REQUESTED",
                        "user": {"login": "alice"},
                        "submitted_at": "2024-01-01T00:00:00Z",
                    }
                ]
            ),
            make_issue(id="issue-9", state="Merging", branch_name="codex/eng-9"),
            "blocking review states",
        ),
        (
            _success_responses(
                issue_comments=[
                    {
                        "id": 1,
                        "body": "## Codex Review — Security",
                        "user": {"login": "github-actions[bot]", "type": "Bot"},
                        "created_at": "2024-01-01T00:00:00Z",
                        "updated_at": "2024-01-01T00:00:00Z",
                    }
                ]
            ),
            make_issue(id="issue-10", state="Merging", branch_name="codex/eng-10"),
            "unresolved review comments",
        ),
    ],
)
async def test_attempt_native_merge_falls_back_for_non_ready_prs(
    tmp_path: Path,
    responses: list[tuple[str, ShellResult]],
    issue,
    message: str,
) -> None:
    workflow = _merging_workflow(tmp_path)
    snapshot = make_snapshot(workflow)
    tracker = MemoryTracker([issue])

    result = await attempt_native_merge(
        issue,
        snapshot,
        tracker,
        shell_capture=_shell_capture_factory(responses, []),
    )

    assert result.merged is False
    assert message in (result.skip_reason or "")
    assert (await tracker.fetch_issue_states_by_ids([issue.id]))[0].state == "Merging"
    assert await tracker.fetch_issue_comments(issue.id) == []


@pytest.mark.asyncio
async def test_attempt_native_merge_requires_branch_metadata(tmp_path: Path) -> None:
    workflow = _merging_workflow(tmp_path)
    snapshot = make_snapshot(workflow)
    issue = make_issue(id="issue-11", state="Merging", branch_name=None)
    tracker = MemoryTracker([issue])

    result = await attempt_native_merge(issue, snapshot, tracker)

    assert result.merged is False
    assert result.skip_reason == "issue has no branch metadata"


@pytest.mark.asyncio
async def test_attempt_native_merge_requires_issue_id(tmp_path: Path) -> None:
    workflow = _merging_workflow(tmp_path)
    snapshot = make_snapshot(workflow)
    issue = make_issue(id=None, state="Merging", branch_name="codex/eng-12")
    tracker = MemoryTracker([])

    result = await attempt_native_merge(issue, snapshot, tracker)

    assert result.merged is False
    assert result.skip_reason == "issue is missing an id"


@pytest.mark.asyncio
async def test_attempt_native_merge_skips_when_repo_root_is_not_git(
    tmp_path: Path,
) -> None:
    workflow = _merging_workflow(tmp_path)
    snapshot = make_snapshot(workflow)
    issue = make_issue(id="issue-12a", state="Merging", branch_name="codex/eng-12a")
    tracker = MemoryTracker([issue])

    async def _capture(command: str, *, cwd: str, env: dict[str, str] | None = None):
        del cwd, env
        if "git rev-parse --is-inside-work-tree" in command:
            return ShellResult(0, "false\n", "")
        raise AssertionError(command)

    result = await attempt_native_merge(
        issue, snapshot, tracker, shell_capture=_capture
    )

    assert result.merged is False
    assert result.skip_reason == "workflow repo root is not a git repository"


@pytest.mark.asyncio
async def test_attempt_native_merge_falls_back_when_merge_command_fails(
    tmp_path: Path,
) -> None:
    workflow = _merging_workflow(tmp_path)
    snapshot = make_snapshot(workflow)
    issue = make_issue(id="issue-12b", state="Merging", branch_name="codex/eng-12b")
    tracker = MemoryTracker([issue])

    result = await attempt_native_merge(
        issue,
        snapshot,
        tracker,
        shell_capture=_shell_capture_factory(
            _success_responses(
                merge_result=ShellResult(1, "", "merge rejected"),
            ),
            [],
        ),
    )

    assert result.merged is False
    assert result.skip_reason == "merge rejected"


@pytest.mark.asyncio
async def test_dispatch_uses_native_merge_fast_path_when_it_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(_merging_workflow(tmp_path))
    tracker = MemoryTracker(
        [make_issue(id="issue-12", identifier="ENG-12", state="Merging")]
    )
    actor = OrchestratorActor(snapshot, tracker_factory=lambda settings: tracker)

    async def _fake_native_merge(issue, workflow_snapshot, tracker_obj):
        del workflow_snapshot
        await tracker_obj.update_issue_state(issue.id, "Done")
        return type("Result", (), {"merged": True, "skip_reason": None})()

    async def _fake_worker_run(self):
        raise AssertionError("worker should not start when native merge succeeds")

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.attempt_native_merge",
        _fake_native_merge,
    )
    monkeypatch.setattr(IssueWorker, "run", _fake_worker_run)

    await actor._dispatch_issue(tracker._issues[0])

    assert actor.running == {}
    assert (await tracker.fetch_issue_states_by_ids(["issue-12"]))[0].state == "Done"


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_worker_when_native_merge_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(_merging_workflow(tmp_path))
    tracker = MemoryTracker(
        [make_issue(id="issue-13", identifier="ENG-13", state="Merging")]
    )
    actor = OrchestratorActor(snapshot, tracker_factory=lambda settings: tracker)
    worker_started = asyncio.Event()

    async def _fake_native_merge(*_args, **_kwargs):
        return type(
            "Result", (), {"merged": False, "skip_reason": "checks are pending"}
        )()

    async def _fake_worker_run(self):
        worker_started.set()

    monkeypatch.setattr(
        "code_factory.runtime.orchestration.dispatching.attempt_native_merge",
        _fake_native_merge,
    )
    monkeypatch.setattr(IssueWorker, "run", _fake_worker_run)

    await actor._dispatch_issue(tracker._issues[0])
    await asyncio.wait_for(worker_started.wait(), timeout=1)

    assert "issue-13" in actor.running


@pytest.mark.asyncio
async def test_native_merge_internal_helpers_cover_edge_paths() -> None:
    async def _status_failure(
        _command: str, *, cwd: str, env: dict[str, str] | None = None
    ):
        del cwd, env
        return ShellResult(1, "", "boom")

    async def _invalid_json(
        _command: str, *, cwd: str, env: dict[str, str] | None = None
    ):
        del cwd, env
        return ShellResult(0, "{", "")

    with pytest.raises(ReviewError, match="boom"):
        await native_merge_module._capture_json(
            "gh api",
            cwd="/tmp",
            shell_capture=_status_failure,
            error_prefix="prefix",
        )
    with pytest.raises(ReviewError, match="prefix: invalid JSON"):
        await native_merge_module._capture_json(
            "gh api",
            cwd="/tmp",
            shell_capture=_invalid_json,
            error_prefix="prefix",
        )
    with pytest.raises(ReviewError, match="invalid PR list payload"):
        await native_merge_module._fetch_pull_request(
            "/tmp",
            "branch",
            shell_capture=_shell_capture_factory(
                [("gh pr list --head", _json_result({"not": "a-list"}))],
                [],
            ),
        )
    with pytest.raises(ReviewError, match="No open PR found"):
        await native_merge_module._fetch_pull_request(
            "/tmp",
            "branch",
            shell_capture=_shell_capture_factory(
                [("gh pr list --head", _json_result([]))],
                [],
            ),
        )
    with pytest.raises(ReviewError, match="invalid PR payload"):
        await native_merge_module._fetch_pull_request(
            "/tmp",
            "branch",
            shell_capture=_shell_capture_factory(
                [
                    ("gh pr list --head", _json_result([{"number": 12}])),
                    ("gh pr view 12 --json", _json_result(["bad"])),
                ],
                [],
            ),
        )
    with pytest.raises(ReviewError, match="head.sha"):
        await native_merge_module._fetch_pr_head(
            "/tmp",
            12,
            shell_capture=_shell_capture_factory(
                [("gh api repos/{owner}/{repo}/pulls/12", _json_result({"head": {}}))],
                [],
            ),
        )
    with pytest.raises(ReviewError, match="head.repo.full_name"):
        await native_merge_module._fetch_pr_head(
            "/tmp",
            12,
            shell_capture=_shell_capture_factory(
                [
                    (
                        "gh api repos/{owner}/{repo}/pulls/12",
                        _json_result({"head": {"sha": "abc123", "repo": {}}}),
                    )
                ],
                [],
            ),
        )

    check_runs = await native_merge_module._get_check_runs(
        "/tmp",
        "fork-owner/fork-repo",
        "abc123",
        shell_capture=_shell_capture_factory(
            [
                (
                    "-f page=1",
                    _json_result({"check_runs": [{"name": "ci"}]}),
                ),
                ("-f page=2", _json_result({"check_runs": []})),
            ],
            [],
        ),
    )
    assert check_runs == [{"name": "ci"}]
    assert (
        await native_merge_module._get_check_runs(
            "/tmp",
            "fork-owner/fork-repo",
            "abc123",
            shell_capture=_shell_capture_factory(
                [("-f page=1", _json_result({"check_runs": []}))],
                [],
            ),
        )
        == []
    )
    assert native_merge_module._merge_command(
        native_merge_module.MergePullRequest(
            number=7,
            url="u",
            head_sha="sha",
            branch_name="branch",
            title="title",
            body="body",
            mergeable="MERGEABLE",
            merge_state="CLEAN",
        )
    ).startswith("gh pr merge 7 --squash --delete-branch")
    with pytest.raises(ReviewError, match="number"):
        native_merge_module._require_int({}, "number")
    with pytest.raises(ReviewError, match="url"):
        native_merge_module._require_str({}, "url")
    assert native_merge_module._optional_str({"body": 1}, "body") is None
    assert native_merge_module._check_timestamp({}) == datetime.min.replace(tzinfo=UTC)
    assert native_merge_module._check_timestamp(
        {"created_at": "2024-01-01T00:00:00Z"}
    ) == datetime.fromisoformat("2024-01-01T00:00:00+00:00")
    assert native_merge_module._dedupe_check_runs(
        [
            {
                "name": "ci",
                "created_at": "2024-01-02T00:00:00Z",
            },
            {
                "name": "ci",
                "created_at": "2024-01-01T00:00:00Z",
            },
        ]
    ) == [{"name": "ci", "created_at": "2024-01-02T00:00:00Z"}]


@pytest.mark.asyncio
async def test_native_merge_readiness_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await native_merge_module._native_merge_readiness_error(
            "/tmp",
            native_merge_module.MergePullRequest(
                number=1,
                url="u",
                head_sha="sha",
                branch_name="branch",
                title="title",
                body="body",
                mergeable="MERGEABLE",
                merge_state=None,
            ),
            shell_capture=lambda *args, **kwargs: None,
        )
        == "PR merge state is unknown"
    )

    async def _head(*_args, **_kwargs):
        return "sha", "fork-owner/fork-repo"

    async def _checks(*_args, **_kwargs):
        return []

    async def _feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(native_merge_module, "_fetch_pr_head", _head)
    monkeypatch.setattr(native_merge_module, "_get_check_runs", _checks)
    monkeypatch.setattr(native_merge_module, "_blocking_feedback_error", _feedback)

    assert (
        await native_merge_module._native_merge_readiness_error(
            "/tmp",
            native_merge_module.MergePullRequest(
                number=1,
                url="u",
                head_sha="sha",
                branch_name="branch",
                title="title",
                body="body",
                mergeable="MERGEABLE",
                merge_state="CLEAN",
            ),
            shell_capture=lambda *args, **kwargs: None,
        )
        == "no check runs reported for the PR head"
    )


def test_native_merge_feedback_helpers_cover_edge_paths() -> None:
    review_requested_at = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
    assert (
        feedback_module.blocking_feedback_error(
            issue_comments=[
                {
                    "id": 1,
                    "body": "[codex] ack",
                    "user": {"login": "codex-user"},
                    "created_at": "2024-01-01T00:01:00Z",
                    "updated_at": "2024-01-01T00:01:00Z",
                },
                {
                    "id": 2,
                    "body": "bot note",
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                    "created_at": "2024-01-01T00:02:00Z",
                    "updated_at": "2024-01-01T00:02:00Z",
                },
            ],
            review_comments=[],
            reviews=[],
        )
        == "PR has unacknowledged Codex review comments"
    )
    assert (
        feedback_module._filter_human_review_comments(
            [
                {
                    "id": 1,
                    "body": "human",
                    "user": {"login": "alice"},
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                },
                {
                    "id": 2,
                    "body": "[codex] ack",
                    "user": {"login": "codex-user"},
                    "in_reply_to_id": 1,
                    "created_at": "2024-01-01T00:01:00Z",
                    "updated_at": "2024-01-01T00:01:00Z",
                },
                {
                    "id": 3,
                    "body": "bot",
                    "user": {"login": "some-bot[bot]", "type": "Bot"},
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                },
            ]
        )
        == []
    )
    assert feedback_module._filter_human_review_comments(
        [
            {
                "id": 4,
                "body": "still open",
                "user": {"login": "alice"},
                "created_at": "2024-01-01T00:03:00Z",
                "updated_at": "2024-01-01T00:03:00Z",
            }
        ]
    ) == [
        {
            "id": 4,
            "body": "still open",
            "user": {"login": "alice"},
            "created_at": "2024-01-01T00:03:00Z",
            "updated_at": "2024-01-01T00:03:00Z",
        }
    ]
    assert feedback_module._filter_codex_comments(
        [
            {
                "id": 1,
                "body": "plain bot",
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "created_at": "2024-01-01T00:02:00Z",
                "updated_at": "2024-01-01T00:02:00Z",
            },
            {
                "id": 2,
                "body": "plain human",
                "user": {"login": "alice"},
                "created_at": "2024-01-01T00:02:00Z",
                "updated_at": "2024-01-01T00:02:00Z",
            },
        ],
        review_requested_at,
    )
    assert (
        feedback_module._filter_codex_comments(
            [
                {
                    "id": 3,
                    "body": "plain bot",
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                    "created_at": "2024-01-01T00:02:00Z",
                    "updated_at": "2024-01-01T00:02:00Z",
                    "in_reply_to_id": 1,
                },
                {
                    "id": 4,
                    "body": "[codex] ack",
                    "user": {"login": "codex-user"},
                    "created_at": "2024-01-01T00:03:00Z",
                    "updated_at": "2024-01-01T00:03:00Z",
                    "in_reply_to_id": 1,
                },
                {
                    "id": 5,
                    "body": "plain bot",
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                    "created_at": "2024-01-01T00:04:00Z",
                    "updated_at": "2024-01-01T00:04:00Z",
                },
                {
                    "id": 6,
                    "body": "[codex] ack",
                    "user": {"login": "codex-user"},
                    "created_at": "2024-01-01T00:05:00Z",
                    "updated_at": "2024-01-01T00:05:00Z",
                },
            ],
            None,
        )
        == []
    )
    assert feedback_module._filter_blocking_reviews(
        [
            {"id": 1, "state": "COMMENTED", "user": {"login": "alice"}},
            {"id": 2, "state": "APPROVED", "user": {"login": "bob"}},
            {"id": 3, "state": "CHANGES_REQUESTED", "user": {"login": "ci-bot[bot]"}},
            {
                "id": 4,
                "state": "CHANGES_REQUESTED",
                "user": {"login": "github-actions[bot]"},
                "submitted_at": "2023-12-31T00:00:00Z",
            },
            {
                "id": 5,
                "state": "PENDING",
                "user": {"login": "carol"},
                "body": "",
                "submitted_at": "2024-01-02T00:00:00Z",
            },
            {"id": 6, "state": "COMMENTED", "user": {}},
        ],
        review_requested_at,
    ) == [
        {
            "id": 3,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "ci-bot[bot]"},
        },
        {
            "id": 5,
            "state": "PENDING",
            "user": {"login": "carol"},
            "body": "",
            "submitted_at": "2024-01-02T00:00:00Z",
        },
    ]
    assert (
        feedback_module._is_blocking_review(
            {
                "state": "CHANGES_REQUESTED",
                "user": {"login": "github-actions[bot]"},
                "submitted_at": "2024-01-02T00:00:00Z",
            },
            review_requested_at,
        )
        is True
    )
    assert feedback_module._latest_codex_reply_by_thread(
        [
            {
                "id": 1,
                "body": "human",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": 2,
                "body": "[codex] ack",
                "created_at": "2024-01-01T00:01:00Z",
                "updated_at": "2024-01-01T00:01:00Z",
            },
            {
                "id": "bad",
                "body": "[codex] ack",
                "created_at": "2024-01-01T00:02:00Z",
                "updated_at": "2024-01-01T00:02:00Z",
            },
        ]
    ) == {
        2: datetime.fromisoformat("2024-01-01T00:01:00+00:00"),
        None: datetime.fromisoformat("2024-01-01T00:02:00+00:00"),
    }
    assert feedback_module._thread_root_id({"id": "bad"}) is None


def test_native_merge_feedback_helpers_cover_remaining_branches() -> None:
    review_requested_at = datetime.fromisoformat("2024-01-01T00:00:00+00:00")

    assert (
        feedback_module._filter_codex_comments(
            [
                {
                    "id": 1,
                    "body": "plain bot before request",
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                    "created_at": "2023-12-31T23:59:00Z",
                    "updated_at": "2023-12-31T23:59:00Z",
                },
                {
                    "id": 2,
                    "body": "plain bot without timestamp",
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                },
            ],
            review_requested_at,
        )
        == []
    )

    threaded_comment = {
        "id": 3,
        "body": "plain bot awaiting reply",
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "created_at": "2024-01-01T00:02:00Z",
        "updated_at": "2024-01-01T00:02:00Z",
        "in_reply_to_id": 99,
    }
    assert feedback_module._filter_codex_comments(
        [threaded_comment],
        review_requested_at=None,
    ) == [threaded_comment]

    latest_reviews = feedback_module._filter_blocking_reviews(
        [
            {
                "id": 10,
                "state": "CHANGES_REQUESTED",
                "user": {"login": "alice"},
                "submitted_at": "2024-01-02T00:00:00Z",
            },
            {
                "id": 11,
                "state": "APPROVED",
                "user": {"login": "alice"},
                "submitted_at": "2024-01-01T00:00:00Z",
            },
        ],
        review_requested_at=None,
    )
    assert latest_reviews == [
        {
            "id": 10,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "alice"},
            "submitted_at": "2024-01-02T00:00:00Z",
        }
    ]

    latest_replies = feedback_module._latest_codex_reply_by_thread(
        [
            {
                "id": 20,
                "body": "[codex] first",
                "created_at": "2024-01-01T00:02:00Z",
                "updated_at": "2024-01-01T00:02:00Z",
            },
            {
                "id": 20,
                "body": "[codex] older duplicate",
                "created_at": "2024-01-01T00:01:00Z",
                "updated_at": "2024-01-01T00:01:00Z",
            },
            {
                "id": 21,
                "body": "[codex] no timestamp",
            },
        ]
    )
    assert latest_replies == {20: datetime.fromisoformat("2024-01-01T00:02:00+00:00")}
