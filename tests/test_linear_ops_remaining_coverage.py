# pyright: reportOptionalSubscript=false

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from code_factory.errors import TrackerClientError
from code_factory.trackers.linear.ops import LinearOps
from code_factory.trackers.linear.ops_files import (
    read_binary_file,
    read_text_file,
    resolve_path,
)
from code_factory.trackers.linear.ops_normalize import normalize_issue
from code_factory.trackers.linear.ops_queries import (
    CREATE_RELATION_MUTATION,
    ISSUE_QUERY,
    ISSUES_QUERY,
)
from code_factory.trackers.linear.ops_resolution import find_exact, find_optional

from .conftest import make_snapshot, write_workflow_file


def _settings(tmp_path: Path):
    return make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md")).settings


class _RemainingOps(LinearOps):
    def __init__(self, settings) -> None:
        async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if query == ISSUE_QUERY:
                return {"data": {"issue": self._issue_payload(str(variables["id"]))}}
            if query == ISSUES_QUERY:
                return {
                    "data": {
                        "issues": {
                            "nodes": [
                                self._issue_payload("ENG-1"),
                                self._issue_payload("ENG-2"),
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    }
                }
            if query == CREATE_RELATION_MUTATION:
                return {"data": {"issueRelationCreate": {"success": True}}}
            raise AssertionError(query)

        super().__init__(settings, graphql)

    def _issue_payload(self, identifier: str) -> dict[str, Any]:
        ticket_number = identifier.split("-")[-1].lower()
        return {
            "id": f"issue-{ticket_number}",
            "identifier": identifier,
            "title": f"Fix {identifier}",
            "description": f"Body for {identifier}",
            "priority": 1,
            "url": f"https://example/{identifier}",
            "branchName": f"codex/{identifier.lower()}",
            "state": {"id": "state-1", "name": "Todo", "type": "unstarted"},
            "project": {
                "id": "project-1",
                "name": "Project",
                "slugId": "project",
                "url": "https://example/project",
            },
            "team": {"id": "team-1", "name": "Team", "key": "ENG"},
            "labels": {"nodes": [{"name": "bug"}]},
            "comments": {"nodes": []},
            "attachments": {"nodes": []},
            "inverseRelations": {"nodes": []},
            "relations": {"nodes": []},
        }

    async def _projects(self) -> list[dict]:
        return [
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
                            "states": {"nodes": [{"id": "state-1", "name": "Todo"}]},
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


def test_linear_file_and_normalize_remaining_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(
            "code_factory.trackers.linear.ops_files.canonicalize",
            lambda _path: (_ for _ in ()).throw(OSError("boom")),
        )
        with pytest.raises(TrackerClientError, match="cannot read `bad.txt`"):
            resolve_path("bad.txt", ())

    empty_text = tmp_path / "empty.txt"
    empty_text.write_text("", encoding="utf-8")
    with pytest.raises(TrackerClientError, match="file is empty"):
        read_text_file(str(empty_text), ())

    missing_binary = tmp_path / "missing.bin"
    with pytest.raises(TrackerClientError, match="cannot read"):
        read_binary_file(str(missing_binary), ())

    empty_binary = tmp_path / "empty.bin"
    empty_binary.write_bytes(b"")
    with pytest.raises(TrackerClientError, match="file is empty"):
        read_binary_file(str(empty_binary), ())

    issue = normalize_issue(
        {
            "id": "issue-1",
            "identifier": "ENG-1",
            "title": "Fix",
            "description": "Body",
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
                        "body": "note",
                        "createdAt": "c",
                        "updatedAt": "u",
                        "resolvedAt": None,
                        "user": {"name": "Bennet"},
                    }
                ]
            },
            "attachments": {
                "nodes": [
                    {
                        "id": "attachment-1",
                        "title": "Log",
                        "subtitle": "build.log",
                        "url": "https://example/file",
                        "sourceType": "upload",
                        "metadata": {"size": 1},
                    }
                ]
            },
            "inverseRelations": {
                "nodes": [
                    {
                        "type": "blocks",
                        "issue": {
                            "id": "issue-2",
                            "identifier": "ENG-2",
                            "title": "Blocked",
                            "state": {
                                "id": "state-1",
                                "name": "Todo",
                                "type": "unstarted",
                            },
                        },
                    },
                    {
                        "type": "related",
                        "issue": {
                            "id": "issue-3",
                            "identifier": "ENG-3",
                            "title": "Related",
                            "state": {
                                "id": "state-1",
                                "name": "Todo",
                                "type": "unstarted",
                            },
                        },
                    },
                ]
            },
            "relations": {
                "nodes": [
                    {
                        "type": "blocks",
                        "relatedIssue": {
                            "id": "issue-4",
                            "identifier": "ENG-4",
                            "title": "Blocking",
                            "state": {
                                "id": "state-1",
                                "name": "Todo",
                                "type": "unstarted",
                            },
                        },
                    },
                    {
                        "type": "related",
                        "relatedIssue": {
                            "id": "issue-5",
                            "identifier": "ENG-5",
                            "title": "Also related",
                            "state": {
                                "id": "state-1",
                                "name": "Todo",
                                "type": "unstarted",
                            },
                        },
                    },
                ]
            },
        },
        include_description=True,
        include_comments=True,
        include_attachments=True,
        include_relations=True,
    )
    assert issue["description"] == "Body"
    assert issue["attachments"][0]["id"] == "attachment-1"
    assert issue["relations"]["blocked_by"][0]["issue"]["identifier"] == "ENG-2"
    assert issue["relations"]["related"][1]["issue"]["identifier"] == "ENG-5"


@pytest.mark.asyncio
async def test_linear_common_and_resolution_remaining_branches(
    tmp_path: Path,
) -> None:
    ops = _RemainingOps(_settings(tmp_path))
    assert (
        await ops._issue_node(
            "ENG-1",
            include_description=False,
            include_comments=False,
            include_attachments=False,
            include_relations=False,
        )
    )["identifier"] == "ENG-1"
    assert (await ops._team_for_issue({"team": {"id": "team-1"}}))["id"] == "team-1"
    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        await ops._team_for_issue({"team": {}})
    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        await ops._team_for_issue({"team": {"id": "missing"}})

    assert (
        ops._matches_issue(
            {"state": {"name": "Todo"}},
            project=None,
            state="Done",
            query=None,
        )
        is False
    )
    assert find_exact([{"id": "x"}, {"id": "ENG-1"}], "ENG-1", "id") == {"id": "ENG-1"}
    assert find_optional([{"id": "ENG-1"}], "ENG-1", "id") == {"id": "ENG-1"}

    await ops._create_relation("ENG-1", "ENG-2", relation_type="blocks")
    assert (
        await ops._issue_input({}, team_node=None, project_node=None, issue_node=None)
        == {}
    )
    team_node, project_node = await ops._resolve_issue_target(None, None)
    assert team_node["id"] == "team-1"
    assert project_node["id"] == "project-1"


@pytest.mark.asyncio
async def test_linear_read_remaining_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _RemainingOps(_settings(tmp_path))
    assert (
        await ops.read_issues(
            project=None,
            state=None,
            query=None,
            limit=0,
            include_description=False,
            include_comments=False,
            include_attachments=False,
            include_relations=False,
        )
    ) == {"issues": [], "count": 0}

    issues = await ops.read_issues(
        project=None,
        state=None,
        query=None,
        limit=3,
        include_description=False,
        include_comments=False,
        include_attachments=False,
        include_relations=False,
    )
    assert issues["count"] == 2
    assert (await ops.read_project("project"))["project"]["slug"] == "project"
    assert (await ops.read_projects(query=None, limit=10))["count"] == 1

    monkeypatch.setattr(
        "code_factory.trackers.linear.ops_read.normalize_team",
        lambda node, *, include_states: None
        if include_states
        else {"id": "team-1", "name": "Team", "key": "ENG"},
    )
    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        await ops.read_states(issue="ENG-1", team=None, project=None)

    monkeypatch.setattr(
        "code_factory.trackers.linear.ops_read.normalize_team",
        lambda node, *, include_states: None,
    )
    with pytest.raises(TrackerClientError, match="tracker_not_found"):
        await ops.read_states(issue=None, team="ENG", project=None)

    monkeypatch.setattr(
        "code_factory.trackers.linear.ops_read.normalize_team",
        lambda node, *, include_states: {
            "id": "team-1",
            "name": "Team",
            "key": "ENG",
            "states": [{"id": "state-1", "name": "Todo"}],
        }
        if include_states
        else {"id": "team-1", "name": "Team", "key": "ENG"},
    )
    states = await ops.read_states(issue=None, team="ENG", project=None)
    assert states["states"][0]["id"] == "state-1"
