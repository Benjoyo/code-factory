from __future__ import annotations

import logging
import os
import shutil

from ..config.models import Settings
from ..errors import WorkspaceError
from ..issues import Issue
from .hooks import run_hook
from .models import Workspace
from .paths import safe_identifier, validate_workspace_path, workspace_path_for_issue
from .utils import ensure_workspace, issue_context

LOGGER = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, settings: Settings):
        self._settings = settings

    @property
    def root(self) -> str:
        return self._settings.workspace.root

    def safe_identifier(self, identifier: str | None) -> str:
        return safe_identifier(identifier)

    def workspace_path_for_issue(self, safe_issue_identifier: str) -> str:
        return workspace_path_for_issue(self.root, safe_issue_identifier)

    def validate_workspace_path(self, workspace: str) -> None:
        validate_workspace_path(self.root, workspace)

    async def create_for_issue(
        self, issue_or_identifier: Issue | str | None
    ) -> Workspace:
        context = issue_context(issue_or_identifier)
        workspace_path = workspace_path_for_issue(
            self.root, context["issue_identifier"]
        )
        validate_workspace_path(self.root, workspace_path)
        created_now = ensure_workspace(workspace_path)
        if created_now and self._settings.hooks.after_create:
            await run_hook(
                self._settings,
                self._settings.hooks.after_create,
                workspace_path,
                context,
                "after_create",
                fatal=True,
            )
        return Workspace(
            path=workspace_path,
            workspace_key=safe_identifier(context["issue_identifier"]),
            created_now=created_now,
        )

    async def run_before_run_hook(
        self, workspace: str, issue_or_identifier: Issue | str | None
    ) -> None:
        if self._settings.hooks.before_run:
            await run_hook(
                self._settings,
                self._settings.hooks.before_run,
                workspace,
                issue_context(issue_or_identifier),
                "before_run",
                fatal=True,
            )

    async def run_after_run_hook(
        self, workspace: str, issue_or_identifier: Issue | str | None
    ) -> None:
        if not self._settings.hooks.after_run:
            return
        try:
            await run_hook(
                self._settings,
                self._settings.hooks.after_run,
                workspace,
                issue_context(issue_or_identifier),
                "after_run",
                fatal=False,
            )
        except WorkspaceError:
            LOGGER.warning("Ignoring after_run hook failure workspace=%s", workspace)

    async def remove(self, workspace: str) -> list[str]:
        if not os.path.exists(workspace):
            return []
        validate_workspace_path(self.root, workspace)
        if os.path.isdir(workspace):
            await self._run_before_remove_hook(workspace)
            shutil.rmtree(workspace, ignore_errors=False)
        else:
            os.unlink(workspace)
        return []

    async def remove_issue_workspaces(self, identifier: str | None) -> None:
        if not isinstance(identifier, str):
            return
        try:
            await self.remove(workspace_path_for_issue(self.root, identifier))
        except (FileNotFoundError, WorkspaceError):
            return

    async def _run_before_remove_hook(self, workspace: str) -> None:
        if not self._settings.hooks.before_remove:
            return
        try:
            await run_hook(
                self._settings,
                self._settings.hooks.before_remove,
                workspace,
                {"issue_id": None, "issue_identifier": os.path.basename(workspace)},
                "before_remove",
                fatal=False,
            )
        except WorkspaceError:
            LOGGER.warning(
                "Ignoring before_remove hook failure workspace=%s", workspace
            )
