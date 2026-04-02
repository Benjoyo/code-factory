# pyright: reportArgumentType=false, reportOptionalSubscript=false

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer
from click.testing import CliRunner

from code_factory.cli import app
from code_factory.errors import TrackerClientError
from code_factory.trackers.linear.client import LinearClient
from code_factory.trackers.linear.ops import LinearOps
from code_factory.trackers.linear.ops.ops_common import LinearOpsCommon
from code_factory.trackers.linear.ops.ops_queries import (
    COMMENT_CREATE_MUTATION,
    COMMENT_UPDATE_MUTATION,
    CREATE_ISSUE_MUTATION,
    CREATE_RELATION_MUTATION,
    FILE_UPLOAD_MUTATION,
    ISSUE_QUERY,
    ISSUES_QUERY,
    LABELS_QUERY,
    UPDATE_ISSUE_MUTATION,
    USERS_QUERY,
)
from code_factory.trackers.linear.queries import QUERY_BY_IDENTIFIER

from ..conftest import make_snapshot, write_workflow_file

runner = CliRunner()


def make_settings(tmp_path: Path):
    return make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md")).settings


def test_tracker_cli_command_matrix_covers_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_workflow_file(tmp_path / "WORKFLOW.md")
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class FakeOps:
        async def read_issue(self, issue: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(("read_issue", (issue,), kwargs))
            return {
                "issue": {
                    "identifier": issue,
                    "title": "Fix",
                    "state": {"name": "Todo"},
                }
            }

        async def read_issues(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("read_issues", (), kwargs))
            return {"issues": [], "count": 0}

        async def create_issue(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("create_issue", (), kwargs))
            return {"issue_id": "issue-1", "created": True}

        async def update_issue(self, issue: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(("update_issue", (issue,), kwargs))
            return {"issue_id": issue, "updated": True}

        async def move_issue(self, issue: str, state: str) -> dict[str, Any]:
            calls.append(("move_issue", (issue, state), {}))
            return {"issue_id": issue, "moved": True}

        async def link_pr(
            self, issue: str, url: str, title: str | None
        ) -> dict[str, Any]:
            calls.append(("link_pr", (issue, url, title), {}))
            return {"issue_id": issue, "linked": True}

        async def list_comments(self, issue: str) -> dict[str, Any]:
            calls.append(("list_comments", (issue,), {}))
            return {"comments": [], "count": 0}

        async def create_comment(self, issue: str, body: str) -> dict[str, Any]:
            calls.append(("create_comment", (issue, body), {}))
            return {"comment_id": "comment-1", "created": True}

        async def update_comment(self, comment_id: str, body: str) -> dict[str, Any]:
            calls.append(("update_comment", (comment_id, body), {}))
            return {"comment_id": comment_id, "updated": True}

        async def get_workpad(self, issue: str) -> dict[str, Any]:
            calls.append(("get_workpad", (issue,), {}))
            return {"found": False}

        async def sync_workpad(
            self, issue: str, *, body: str | None = None, file_path: str | None = None
        ) -> dict[str, Any]:
            calls.append(
                ("sync_workpad", (issue,), {"body": body, "file_path": file_path})
            )
            return {"comment_id": "comment-1", "created": True}

        async def raw_graphql(
            self, query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            calls.append(("raw_graphql", (query,), {"variables": variables}))
            return {"data": {"viewer": {"id": "viewer-1"}}}

        async def close(self) -> None:
            calls.append(("close", (), {}))

    monkeypatch.setattr(
        "code_factory.trackers.cli.build_tracker_ops",
        lambda _settings, *, allowed_roots: FakeOps(),
    )

    commands = [
        ["issue", "list", "--json"],
        ["issue", "create", "Title", "--json"],
        ["issue", "update", "ENG-1", "--title", "Title", "--json"],
        ["issue", "move", "ENG-1", "Done", "--json"],
        [
            "issue",
            "link-pr",
            "ENG-1",
            "https://example/pr/1",
            "--title",
            "PR",
            "--json",
        ],
        ["comment", "list", "ENG-1", "--json"],
        ["comment", "create", "ENG-1", "--body", "hello", "--json"],
        ["comment", "update", "comment-1", "--body", "hello", "--json"],
        ["workpad", "get", "ENG-1", "--json"],
        [
            "tracker",
            "raw",
            "--query",
            "query Viewer { viewer { id } }",
            "--variables",
            '{"x":1}',
            "--json",
        ],
    ]
    for command in commands:
        result = runner.invoke(typer.main.get_command(app), command)
        assert result.exit_code == 0, result.output

    invoked = [name for name, _args, _kwargs in calls]
    assert "read_issues" in invoked
    assert "create_issue" in invoked
    assert "update_issue" in invoked
    assert "move_issue" in invoked
    assert "link_pr" in invoked
    assert "list_comments" in invoked
    assert "create_comment" in invoked
    assert "update_comment" in invoked
    assert "get_workpad" in invoked
    assert "raw_graphql" in invoked


@pytest.mark.asyncio
async def test_linear_client_fetch_issue_identifier_error_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    errors_client = LinearClient(
        settings,
        request_fun=lambda query, variables, operation_name=None: (
            (_ for _ in ()).throw(AssertionError("request_fun should not be used"))
        ),
        client_factory=lambda: type(
            "Client",
            (),
            {
                "request": staticmethod(
                    lambda query, variables, operation_name=None: __import__(
                        "asyncio"
                    ).sleep(0, result={"errors": [{"message": "boom"}]})
                ),
                "close": staticmethod(lambda: __import__("asyncio").sleep(0)),
            },
        )(),
    )
    with pytest.raises(TrackerClientError, match="linear_graphql_errors"):
        await errors_client.fetch_issue_by_identifier("ENG-1")

    unknown_client = LinearClient(
        settings,
        client_factory=lambda: type(
            "Client",
            (),
            {
                "request": staticmethod(
                    lambda query, variables, operation_name=None: __import__(
                        "asyncio"
                    ).sleep(0, result={"data": {"issue": "bad"}})
                ),
                "close": staticmethod(lambda: __import__("asyncio").sleep(0)),
            },
        )(),
    )
    with pytest.raises(TrackerClientError, match="linear_unknown_payload"):
        await unknown_client.fetch_issue_by_identifier("ENG-1")


@pytest.mark.asyncio
async def test_linear_common_resolution_and_read_write_helpers_cover_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)

    class CoverageOps(LinearOps):
        def __init__(self) -> None:
            async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
                if query == QUERY_BY_IDENTIFIER:
                    return {"data": {"issue": {"id": "issue-1"}}}
                if query == CREATE_RELATION_MUTATION:
                    return {"data": {"issueRelationCreate": {"success": True}}}
                if query == LABELS_QUERY:
                    return {
                        "data": {
                            "issueLabels": {"nodes": [{"id": "label-1", "name": "bug"}]}
                        }
                    }
                if query == USERS_QUERY:
                    return {
                        "data": {
                            "users": {"nodes": [{"id": "user-1", "name": "Bennet"}]}
                        }
                    }
                if query == UPDATE_ISSUE_MUTATION:
                    return {"data": {"issueUpdate": {"issue": {"id": "issue-1"}}}}
                if "CodeFactoryTrackerProjectByName" in query:
                    return {
                        "data": {
                            "projects": {
                                "nodes": [
                                    {
                                        "id": "project-1",
                                        "name": "Project",
                                        "slugId": "project",
                                        "url": "https://example/project",
                                        "teams": {
                                            "nodes": [
                                                {
                                                    "id": "team-1",
                                                    "name": "Team",
                                                    "key": "ENG",
                                                }
                                            ]
                                        },
                                    }
                                ]
                            }
                        }
                    }
                if query == ISSUES_QUERY:
                    return {
                        "data": {
                            "issues": {
                                "nodes": [
                                    {
                                        "id": "issue-1",
                                        "identifier": "ENG-1",
                                        "title": "Fix",
                                        "priority": 1,
                                        "url": "https://example/ENG-1",
                                        "branchName": "codex/eng-1",
                                        "state": {
                                            "id": "state-1",
                                            "name": "Todo",
                                            "type": "unstarted",
                                        },
                                        "project": {
                                            "id": "project-1",
                                            "name": "Project",
                                            "slugId": "project",
                                            "url": "https://example/project",
                                        },
                                        "team": {
                                            "id": "team-1",
                                            "name": "Team",
                                            "key": "ENG",
                                        },
                                        "labels": {"nodes": [{"name": "bug"}]},
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "id": "comment-1",
                                                    "body": "note",
                                                    "createdAt": "c",
                                                    "updatedAt": "u",
                                                    "resolvedAt": None,
                                                    "user": {"name": "B"},
                                                }
                                            ]
                                        },
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                if query == COMMENT_CREATE_MUTATION:
                    return {
                        "data": {
                            "commentCreate": {
                                "comment": {
                                    "id": "comment-1",
                                    "url": "https://example/comment-1",
                                }
                            }
                        }
                    }
                if query == COMMENT_UPDATE_MUTATION:
                    return {
                        "data": {
                            "commentUpdate": {
                                "comment": {
                                    "id": "comment-2",
                                    "url": "https://example/comment-2",
                                }
                            }
                        }
                    }
                if query == CREATE_ISSUE_MUTATION:
                    return {
                        "data": {
                            "issueCreate": {
                                "issue": {
                                    "id": "issue-2",
                                    "identifier": "ENG-2",
                                    "title": "Created",
                                    "url": "https://example/ENG-2",
                                }
                            }
                        }
                    }
                if query == FILE_UPLOAD_MUTATION:
                    return {
                        "data": {
                            "fileUpload": {
                                "uploadFile": {"uploadUrl": "", "assetUrl": ""}
                            }
                        }
                    }
                raise AssertionError(query)

            super().__init__(settings, graphql)

        async def _projects(self) -> list[dict]:
            return [
                {
                    "id": "project-1",
                    "name": "Project",
                    "slugId": "project",
                    "teams": {
                        "nodes": [
                            {
                                "id": "team-1",
                                "name": "Team",
                                "key": "ENG",
                                "states": {
                                    "nodes": [{"id": "state-1", "name": "Todo"}]
                                },
                            }
                        ]
                    },
                }
            ]

        async def _teams(self) -> list[dict]:
            return [
                {
                    "id": "team-1",
                    "name": "Team",
                    "key": "ENG",
                    "states": {"nodes": [{"id": "state-1", "name": "Todo"}]},
                }
            ]

        async def _team_for_issue(self, issue_node: dict) -> dict:
            return (await self._teams())[0]

        async def _issue_node(
            self,
            issue: str,
            *,
            include_description: bool,
            include_comments: bool,
            include_attachments: bool,
            include_relations: bool,
        ) -> dict:
            return {
                "id": "issue-1",
                "identifier": "ENG-1",
                "title": "Fix",
                "priority": 1,
                "url": "https://example/ENG-1",
                "branchName": "codex/eng-1",
                "state": {"id": "state-1", "name": "Todo", "type": "unstarted"},
                "project": {
                    "id": "project-1",
                    "name": "Project",
                    "slugId": "project",
                    "url": "https://example/project",
                },
                "team": {"id": "team-1", "name": "Team", "key": "ENG"},
                "labels": {"nodes": [{"name": "bug"}]},
                "comments": {
                    "nodes": [
                        {
                            "id": "comment-1",
                            "body": "## Codex Workpad\nbody",
                            "createdAt": "c",
                            "updatedAt": "u",
                            "resolvedAt": None,
                            "user": {"name": "B"},
                        }
                    ]
                },
            }

    ops = CoverageOps()

    assert await ops._resolve_issue_id("ENG-1") == "issue-1"
    assert await ops._team_with_states(None) is None
    assert await ops._team_with_states({"name": "Team"}) == {"name": "Team"}
    assert await ops._team_with_states({"id": "team-1", "name": "Team"}) == {
        "id": "team-1",
        "name": "Team",
        "key": "ENG",
        "states": {"nodes": [{"id": "state-1", "name": "Todo"}]},
    }
    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        await LinearOpsCommon(
            settings,
            lambda query, variables: __import__("asyncio").sleep(
                0, result={"data": {"issue": []}}
            ),
        )._issue_node(
            "ENG-404",
            include_description=False,
            include_comments=False,
            include_attachments=False,
            include_relations=False,
        )
    with pytest.raises(TrackerClientError, match="tracker operation failed"):
        ops._data({"errors": [{"message": "tracker operation failed"}]}, "issue")
    assert (
        ops._matches_issue(
            {"project": {"id": "project-1"}, "state": {"name": "Todo"}},
            project="project-1",
            state="Todo",
            query=None,
        )
        is True
    )
    assert (
        ops._matches_issue(
            {"identifier": "ENG-1", "title": "Fix", "description": ""},
            project=None,
            state=None,
            query="missing",
        )
        is False
    )
    assert ops._error_message([{}]) == "unknown tracker error"

    assert await ops._issue_input(
        {
            "title": "Issue",
            "description": "Body",
            "priority": 2,
            "state": "Todo",
            "assignee": "Bennet",
            "labels": ["bug"],
        },
        team_node={
            "id": "team-1",
            "states": {"nodes": [{"id": "state-1", "name": "Todo"}]},
        },
        project_node={"id": "project-1"},
        issue_node=None,
    ) == {
        "title": "Issue",
        "description": "Body",
        "priority": 2,
        "projectId": "project-1",
        "teamId": "team-1",
        "stateId": "state-1",
        "assigneeId": "user-1",
        "labelIds": ["label-1"],
    }

    relations: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        ops,
        "_create_relation",
        lambda source, related, *, relation_type: __import__("asyncio").sleep(
            0, result=relations.append((source, related, relation_type))
        ),
    )
    await ops._apply_relations(None, {"blocked_by": ["ENG-2"]})
    await ops._apply_relations(
        "ENG-1",
        {"blocked_by": ["ENG-2"], "blocks": ["ENG-3"], "related_to": ["ENG-4"]},
    )
    assert relations == [
        ("ENG-2", "ENG-1", "blocks"),
        ("ENG-1", "ENG-3", "blocks"),
        ("ENG-1", "ENG-4", "related"),
    ]

    class FailingRelationOps(CoverageOps):
        async def _resolve_issue_id(self, issue: str) -> str:
            return issue

        async def raw_graphql(self, query: str, variables: dict | None = None) -> dict:
            return {}

    failing_relation_ops = FailingRelationOps()
    monkeypatch.setattr(
        failing_relation_ops,
        "_graphql",
        lambda query, variables: __import__("asyncio").sleep(
            0, result={"data": {"issueRelationCreate": {"success": False}}}
        ),
    )
    with pytest.raises(TrackerClientError, match="tracker relation update failed"):
        await failing_relation_ops._create_relation(
            "ENG-1", "ENG-2", relation_type="blocks"
        )

    team_node, project_node = await ops._resolve_issue_target("project", "ENG")
    assert team_node["id"] == "team-1"
    assert project_node["id"] == "project-1"
    assert await ops._resolve_team(team=None, project=None) == {
        "id": "team-1",
        "name": "Team",
        "key": "ENG",
    }
    no_project_settings = make_snapshot(
        write_workflow_file(
            tmp_path / "NO_PROJECT_TEAM.md",
            tracker={"project": None},
        )
    ).settings
    assert (
        await LinearOps(
            no_project_settings,
            lambda query, variables: __import__("asyncio").sleep(0, result={}),
        )._resolve_team(team=None, project=None)
        is None
    )
    assert (
        await ops._resolve_state_id(
            "Todo",
            issue_node=None,
            team_node=None,
            project_node={
                "name": "Project",
                "teams": {
                    "nodes": [
                        {
                            "id": "team-1",
                            "states": {"nodes": [{"id": "state-1", "name": "Todo"}]},
                        }
                    ]
                },
            },
        )
        == "state-1"
    )
    with pytest.raises(TrackerClientError, match="`state` requires a resolvable team"):
        await LinearOps(
            settings, lambda query, variables: __import__("asyncio").sleep(0, result={})
        )._resolve_state_id("Todo", issue_node=None, team_node=None, project_node=None)
    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        no_team_ops = LinearOps(
            settings, lambda query, variables: __import__("asyncio").sleep(0, result={})
        )
        monkeypatch.setattr(
            no_team_ops,
            "_team_with_states",
            lambda team_node: __import__("asyncio").sleep(0, result=None),
        )
        await no_team_ops._resolve_state_id(
            "Todo",
            issue_node=None,
            team_node={"id": "team-1"},
            project_node=None,
        )
    assert await ops._resolve_label_ids(["bug"]) == ["label-1"]
    assert await ops._resolve_user_id("Bennet") == "user-1"
    assert ops._string_list("bad") == []
    assert ops._string_list([1, "x"]) == ["1", "x"]
    assert await ops._update_issue_state("issue-1", state_id="state-1") == {
        "id": "issue-1"
    }

    issue_payload = await ops.read_issue(
        "ENG-1",
        include_description=False,
        include_comments=False,
        include_attachments=False,
        include_relations=False,
    )
    assert issue_payload["issue"]["identifier"] == "ENG-1"
    assert (await ops.read_projects(query="proj", limit=10))["count"] == 1
    assert (await ops.read_states(issue="ENG-1", team=None, project=None))["states"][0][
        "name"
    ] == "Todo"
    with pytest.raises(
        TrackerClientError,
        match="one of `issue`, `team`, or a default workflow project is required",
    ):
        empty_ops = LinearOps(
            no_project_settings,
            lambda query, variables: __import__("asyncio").sleep(0, result={}),
        )
        monkeypatch.setattr(
            empty_ops,
            "_resolve_team",
            lambda *, team, project: __import__("asyncio").sleep(0, result=None),
        )
        await empty_ops.read_states(issue=None, team=None, project=None)
    assert (await ops.list_comments("ENG-1"))["count"] == 1
    assert (await ops.get_workpad("ENG-1"))["found"] is True

    monkeypatch.setattr(
        ops,
        "list_comments",
        lambda issue: __import__("asyncio").sleep(
            0,
            result={
                "comments": [
                    {
                        "id": "comment-1",
                        "body": "plain text",
                        "created_at": None,
                        "updated_at": None,
                        "resolved_at": "done",
                    }
                ]
            },
        ),
    )
    assert (await ops.get_workpad("ENG-1"))["found"] is False
    with pytest.raises(TrackerClientError, match="missing end cursor"):
        ops._next_page_cursor({"pageInfo": {"hasNextPage": True, "endCursor": None}})

    assert await ops.create_comment("ENG-1", "hello") == {
        "comment_id": "comment-1",
        "url": "https://example/comment-1",
        "created": True,
    }
    assert await ops.update_comment("comment-1", "hello") == {
        "comment_id": "comment-2",
        "url": "https://example/comment-2",
        "updated": True,
    }

    monkeypatch.setattr(
        ops,
        "get_workpad",
        lambda issue: __import__("asyncio").sleep(0, result={"found": False}),
    )
    monkeypatch.setattr(
        ops,
        "create_comment",
        lambda issue, body: __import__("asyncio").sleep(
            0, result={"comment_id": "comment-1"}
        ),
    )
    monkeypatch.setattr(
        "code_factory.trackers.linear.ops.ops_write.read_text_file",
        lambda path, allowed_roots: "body",
    )
    assert await ops.sync_workpad("ENG-1", file_path="workpad.md") == {
        "comment_id": "comment-1",
        "created": True,
    }

    monkeypatch.setattr(
        ops,
        "get_workpad",
        lambda issue: __import__("asyncio").sleep(
            0, result={"found": True, "comment_id": "comment-1"}
        ),
    )
    monkeypatch.setattr(
        ops,
        "update_comment",
        lambda comment_id, body: __import__("asyncio").sleep(
            0, result={"comment_id": comment_id}
        ),
    )
    assert await ops.sync_workpad("ENG-1", body="body") == {
        "comment_id": "comment-1",
        "created": False,
    }

    monkeypatch.setattr(
        ops,
        "_update_issue_state",
        lambda issue_id, *, state_id: __import__("asyncio").sleep(0, result={}),
    )
    move_result = await ops.move_issue("ENG-1", "Todo")
    assert move_result["issue_id"] == "issue-1"
    assert move_result["moved"] is True

    with pytest.raises(
        TrackerClientError, match="`team` is required to create an issue"
    ):
        await LinearOps(
            no_project_settings,
            lambda query, variables: __import__("asyncio").sleep(0, result={}),
        ).create_issue(title="Issue")

    monkeypatch.setattr(
        ops,
        "_resolve_issue_target",
        lambda project, team: __import__("asyncio").sleep(
            0, result=({"id": "team-1"}, {"id": "project-1"})
        ),
    )
    monkeypatch.setattr(
        ops,
        "_issue_input",
        lambda values, *, team_node, project_node, issue_node: __import__(
            "asyncio"
        ).sleep(0, result={"title": "Issue"}),
    )
    monkeypatch.setattr(
        ops, "_apply_relations", lambda issue_id, values: __import__("asyncio").sleep(0)
    )
    assert (await ops.create_issue(title="Issue"))["created"] is True
    assert (await ops.update_issue("ENG-1", title="Issue", project="project"))[
        "updated"
    ] is True

    monkeypatch.setattr(
        ops,
        "_resolve_issue_id",
        lambda issue: __import__("asyncio").sleep(0, result="issue-1"),
    )
    monkeypatch.setattr(
        ops,
        "_graphql",
        lambda query, variables: __import__("asyncio").sleep(
            0, result={"data": {"attachmentLinkGitHubPR": {"success": True}}}
        ),
    )
    assert (await ops.link_pr("ENG-1", "https://example/pr/1", None))["linked"] is True

    file_path = tmp_path / "image.bin"
    file_path.write_bytes(b"bin")
    monkeypatch.setattr(
        "code_factory.trackers.linear.ops.ops_write.read_binary_file",
        lambda path, allowed_roots: ("image.bin", b"bin", "application/octet-stream"),
    )
    with pytest.raises(TrackerClientError, match="did not return usable upload URLs"):
        await ops.upload_file(str(file_path))


@pytest.mark.asyncio
async def test_update_issue_skips_project_resolution_for_description_only_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    graphql_calls: list[tuple[str, dict[str, Any]]] = []

    class DescriptionOnlyUpdateOps(LinearOps):
        def __init__(self) -> None:
            async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
                graphql_calls.append((query, variables))
                if query == ISSUE_QUERY:
                    return {
                        "data": {
                            "issue": {
                                "id": "issue-1",
                                "identifier": "ENG-1",
                                "title": "Fix",
                                "url": "https://example/ENG-1",
                            }
                        }
                    }
                if query == UPDATE_ISSUE_MUTATION:
                    return {
                        "data": {
                            "issueUpdate": {
                                "issue": {
                                    "id": "issue-1",
                                    "identifier": "ENG-1",
                                    "title": "Fix",
                                    "url": "https://example/ENG-1",
                                }
                            }
                        }
                    }
                raise AssertionError(query)

            super().__init__(settings, graphql)

    ops = DescriptionOnlyUpdateOps()

    async def _unexpected_issue_target(
        _project: object, _team: object
    ) -> tuple[dict | None, dict | None]:
        raise AssertionError("_resolve_issue_target should not run")

    monkeypatch.setattr(ops, "_resolve_issue_target", _unexpected_issue_target)

    assert await ops.update_issue("ENG-1", description="Updated scope") == {
        "issue_id": "issue-1",
        "identifier": "ENG-1",
        "title": "Fix",
        "url": "https://example/ENG-1",
        "updated": True,
    }
    assert graphql_calls == [
        (
            ISSUE_QUERY,
            {
                "id": "ENG-1",
                "includeDescription": False,
                "includeComments": False,
                "includeAttachments": False,
                "includeRelations": False,
            },
        ),
        (
            UPDATE_ISSUE_MUTATION,
            {"id": "issue-1", "input": {"description": "Updated scope"}},
        ),
    ]
