"""Write-side normalized Linear ticket operations."""

from __future__ import annotations

import httpx

from ...errors import TrackerClientError
from .ops_files import read_binary_file, read_text_file
from .ops_normalize import normalize_state
from .ops_queries import (
    ATTACH_LINK_FALLBACK_MUTATION,
    ATTACH_PR_MUTATION,
    COMMENT_CREATE_MUTATION,
    COMMENT_UPDATE_MUTATION,
    CREATE_ISSUE_MUTATION,
    FILE_UPLOAD_MUTATION,
    UPDATE_ISSUE_MUTATION,
)
from .ops_read import LinearOpsReadMixin


class LinearOpsWriteMixin(LinearOpsReadMixin):
    """Comment, issue, PR, upload, and workpad write operations."""

    async def create_comment(self, issue: str, body: str) -> dict:
        response = await self._graphql(
            COMMENT_CREATE_MUTATION,
            {"issueId": await self._resolve_issue_id(issue), "body": body},
        )
        comment = (self._data(response, "commentCreate") or {}).get("comment") or {}
        return {
            "comment_id": comment.get("id"),
            "url": comment.get("url"),
            "created": True,
        }

    async def update_comment(self, comment_id: str, body: str) -> dict:
        response = await self._graphql(
            COMMENT_UPDATE_MUTATION,
            {"commentId": comment_id, "body": body},
        )
        comment = (self._data(response, "commentUpdate") or {}).get("comment") or {}
        return {
            "comment_id": comment.get("id") or comment_id,
            "url": comment.get("url"),
            "updated": True,
        }

    async def sync_workpad(
        self,
        issue: str,
        *,
        body: str | None = None,
        file_path: str | None = None,
    ) -> dict:
        text = (
            body
            if body is not None
            else read_text_file(file_path or "", self._allowed_roots)
        )
        workpad = await self.get_workpad(issue)
        if workpad["found"]:
            result = await self.update_comment(workpad["comment_id"], text)
            result["created"] = False
            return result
        result = await self.create_comment(issue, text)
        result["created"] = True
        return result

    async def move_issue(self, issue: str, state: str) -> dict:
        issue_node = await self._issue_node(
            issue,
            include_description=False,
            include_comments=False,
            include_attachments=False,
            include_relations=False,
        )
        updated = await self._update_issue_state(
            str(issue_node.get("id")),
            state_id=await self._resolve_state_id(
                state,
                issue_node=issue_node,
                team_node=None,
                project_node=None,
            ),
        )
        return {
            "issue_id": updated.get("id") or issue_node.get("id"),
            "identifier": updated.get("identifier"),
            "state": normalize_state(updated.get("state")),
            "moved": True,
        }

    async def create_issue(self, **kwargs: object) -> dict:
        team_node, project_node = await self._resolve_issue_target(
            kwargs.get("project"),
            kwargs.get("team"),
        )
        if team_node is None:
            raise TrackerClientError(
                ("tracker_missing_field", "`team` is required to create an issue")
            )
        response = await self._graphql(
            CREATE_ISSUE_MUTATION,
            {
                "input": await self._issue_input(
                    kwargs,
                    team_node=team_node,
                    project_node=project_node,
                    issue_node=None,
                )
            },
        )
        issue = (self._data(response, "issueCreate") or {}).get("issue") or {}
        await self._apply_relations(issue.get("id"), kwargs)
        return {
            "issue_id": issue.get("id"),
            "identifier": issue.get("identifier"),
            "title": issue.get("title"),
            "url": issue.get("url"),
            "created": True,
        }

    async def update_issue(self, issue: str, **kwargs: object) -> dict:
        issue_node = await self._issue_node(
            issue,
            include_description=False,
            include_comments=False,
            include_attachments=False,
            include_relations=False,
        )
        team_node, project_node = await self._resolve_issue_target(
            kwargs.get("project"),
            kwargs.get("team"),
        )
        response = await self._graphql(
            UPDATE_ISSUE_MUTATION,
            {
                "id": issue_node.get("id"),
                "input": await self._issue_input(
                    kwargs,
                    team_node=team_node,
                    project_node=project_node,
                    issue_node=issue_node,
                ),
            },
        )
        updated = (self._data(response, "issueUpdate") or {}).get("issue") or {}
        await self._apply_relations(issue_node.get("id"), kwargs)
        return {
            "issue_id": updated.get("id") or issue_node.get("id"),
            "identifier": updated.get("identifier") or issue_node.get("identifier"),
            "title": updated.get("title") or issue_node.get("title"),
            "url": updated.get("url") or issue_node.get("url"),
            "updated": True,
        }

    async def link_pr(self, issue: str, url: str, title: str | None) -> dict:
        issue_id = await self._resolve_issue_id(issue)
        response = await self._graphql(
            ATTACH_PR_MUTATION,
            {"issueId": issue_id, "url": url, "title": title},
        )
        attach = (
            (response.get("data", {}) or {}).get("attachmentLinkGitHubPR") or {}
            if isinstance(response, dict)
            else {}
        )
        if (
            response.get("errors") if isinstance(response, dict) else None
        ) or attach.get("success") is not True:
            fallback = await self._graphql(
                ATTACH_LINK_FALLBACK_MUTATION,
                {"issueId": issue_id, "url": url, "title": title or "PR"},
            )
            fallback_attach = (
                (self._data(fallback, "attachmentCreate") or {})
                if isinstance(fallback, dict)
                else {}
            )
            if fallback_attach.get("success") is not True:
                raise TrackerClientError(
                    ("tracker_operation_failed", "tracker PR link attachment failed")
                )
        return {"issue_id": issue_id, "url": url, "title": title, "linked": True}

    async def upload_file(self, file_path: str) -> dict:
        filename, content, content_type = read_binary_file(
            file_path,
            self._allowed_roots,
        )
        response = await self._graphql(
            FILE_UPLOAD_MUTATION,
            {"filename": filename, "contentType": content_type, "size": len(content)},
        )
        upload = (self._data(response, "fileUpload") or {}).get("uploadFile") or {}
        if not upload.get("uploadUrl") or not upload.get("assetUrl"):
            raise TrackerClientError(
                (
                    "tracker_operation_failed",
                    "tracker file upload did not return usable upload URLs",
                )
            )
        upload_url = str(upload["uploadUrl"])
        asset_url = str(upload["assetUrl"])
        headers = {"Content-Type": content_type, "Cache-Control": "public, max-age=31536000"}
        headers.update(
            {
                item["key"]: item["value"]
                for item in upload.get("headers") or []
                if isinstance(item, dict)
                and isinstance(item.get("key"), str)
                and isinstance(item.get("value"), str)
            }
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                result = await client.put(
                    upload_url,
                    headers=headers,
                    content=content,
                )
                result.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text.strip()
                detail = (
                    f"tracker file upload PUT failed with HTTP {exc.response.status_code}: {body}"
                    if body
                    else f"tracker file upload PUT failed with HTTP {exc.response.status_code}"
                )
                raise TrackerClientError(("tracker_operation_failed", detail)) from exc
        return {
            "filename": filename,
            "content_type": content_type,
            "asset_url": asset_url,
            "markdown": f"![{filename}]({asset_url})",
        }
