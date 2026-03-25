from __future__ import annotations

"""Manages App Server processes and shields the rest of the codebase from IPC details."""

import asyncio
import os
from typing import Any

from ....config.models import CodingAgentSettings, WorkspaceSettings
from ....errors import AppServerError, ConfigValidationError, WorkspaceError
from ....issues import Issue
from ....runtime.subprocess import ProcessTree
from ....structured_results import StructuredTurnResult
from ....workspace.paths import canonicalize, validate_workspace_path
from ..config import build_launch_command
from ..tools import DynamicToolExecutor
from .messages import default_on_message, emit_message
from .protocol import send_initialize, start_thread, start_turn, steer_turn
from .routing import route_stdout
from .session import AppServerSession
from .streams import stderr_reader, stdout_reader, wait_for_exit
from .turns import await_turn_completion


class AppServerClient:
    """Low-level client orchestrating the Codex app-server subprocess lifecycle."""

    def __init__(
        self,
        coding_agent: CodingAgentSettings,
        workspace: WorkspaceSettings,
        *,
        dynamic_tool_factory=None,
    ) -> None:
        self._coding_agent = coding_agent
        self._workspace = workspace
        self._dynamic_tool_factory = dynamic_tool_factory

    async def run(
        self,
        workspace: str,
        prompt: str,
        issue: Issue,
        *,
        on_message=None,
        tool_executor: DynamicToolExecutor | None = None,
    ) -> StructuredTurnResult:
        session = await self.start_session(workspace)
        try:
            return await self.run_turn(
                session,
                prompt,
                issue,
                on_message=on_message,
                tool_executor=tool_executor,
            )
        finally:
            await session.stop()

    async def start_session(self, workspace: str) -> AppServerSession:
        """Spin up the Codex runtime and bootstrap a messaging thread before use."""
        validated_workspace = self._validate_workspace_cwd(workspace)
        try:
            launch_command = build_launch_command(
                self._coding_agent, validated_workspace
            )
        except ConfigValidationError as exc:
            raise AppServerError(str(exc)) from exc
        process_tree = await ProcessTree.spawn_shell(
            launch_command,
            cwd=validated_workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        session = await self._bootstrap_session(process_tree, validated_workspace)
        return session

    async def run_turn(
        self,
        session: AppServerSession,
        prompt: str,
        issue: Issue,
        *,
        on_message=None,
        tool_executor: DynamicToolExecutor | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> StructuredTurnResult:
        handler = on_message or default_on_message
        executor = tool_executor or self._build_tool_executor(session.workspace, issue)
        turn_id = await start_turn(
            session,
            prompt,
            issue,
            output_schema=output_schema,
        )
        session_id = f"{session.thread_id}-{turn_id}"
        await emit_message(
            handler,
            "session_started",
            {
                "session_id": session_id,
                "thread_id": session.thread_id,
                "turn_id": turn_id,
            },
            {"runtime_pid": session.runtime_pid},
        )
        try:
            return await await_turn_completion(session, handler, executor)
        except Exception as exc:
            await emit_message(
                handler,
                "turn_ended_with_error",
                {"session_id": session_id, "reason": repr(exc)},
                {"runtime_pid": session.runtime_pid},
            )
            raise

    async def steer(self, session: AppServerSession, message: str) -> str:
        """Forward a steering message to the active in-flight turn."""

        return await steer_turn(session, message)

    async def _bootstrap_session(
        self, process_tree: ProcessTree, workspace: str
    ) -> AppServerSession:
        """Wire stdout/stderr readers and register the new thread with Codex."""
        stdout_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        assert process_tree.process.stdout is not None
        assert process_tree.process.stderr is not None
        stdout_task = asyncio.create_task(
            stdout_reader(process_tree.process.stdout, stdout_queue)
        )
        stderr_task = asyncio.create_task(stderr_reader(process_tree.process.stderr))
        wait_task = asyncio.create_task(wait_for_exit(process_tree, stdout_queue))
        approval_policy = self._coding_agent.approval_policy
        turn_sandbox_policy = self._resolve_turn_sandbox_policy(workspace)
        thread_sandbox = self._coding_agent.thread_sandbox
        try:
            await send_initialize(
                stdout_queue,
                process_tree,
                default_timeout_ms=self._coding_agent.read_timeout_ms,
            )
            thread_id = await start_thread(
                stdout_queue,
                process_tree,
                workspace,
                approval_policy,
                thread_sandbox,
                default_timeout_ms=self._coding_agent.read_timeout_ms,
            )
        except Exception:
            await process_tree.terminate()
            stdout_task.cancel()
            stderr_task.cancel()
            wait_task.cancel()
            raise
        routing_task = asyncio.create_task(
            route_stdout(stdout_queue, event_queue, pending_requests)
        )
        return AppServerSession(
            process_tree=process_tree,
            workspace=workspace,
            approval_policy=approval_policy,
            thread_sandbox=thread_sandbox,
            turn_sandbox_policy=turn_sandbox_policy,
            thread_id=thread_id,
            read_timeout_ms=self._coding_agent.read_timeout_ms,
            turn_timeout_ms=self._coding_agent.turn_timeout_ms,
            auto_approve_requests=approval_policy == "never",
            stdout_queue=stdout_queue,
            event_queue=event_queue,
            pending_requests=pending_requests,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
            wait_task=wait_task,
            routing_task=routing_task,
        )

    def _build_tool_executor(self, workspace: str, issue: Issue) -> DynamicToolExecutor:
        """Provide a DynamicToolExecutor bound to the workspace and tracker tools."""
        if self._dynamic_tool_factory is None:

            class MissingToolBackend:
                async def __call__(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
                    raise AppServerError("missing_dynamic_tool_backend")

                def __getattr__(self, _name: str) -> Any:
                    return self.__call__

            return DynamicToolExecutor(
                MissingToolBackend(),
                allowed_roots=(workspace,),
                current_issue=issue.identifier,
            )
        return self._dynamic_tool_factory(workspace, issue)

    def _validate_workspace_cwd(self, workspace: str) -> str:
        """Check the workspace path is inside the configured workspace root."""
        expanded_workspace = os.path.abspath(os.path.expanduser(workspace))
        expanded_root = os.path.abspath(os.path.expanduser(self._workspace.root))
        canonical_workspace = canonicalize(expanded_workspace)
        canonical_root = canonicalize(expanded_root)
        try:
            return validate_workspace_path(canonical_root, canonical_workspace)
        except WorkspaceError as exc:
            reason = exc.reason if isinstance(exc.reason, tuple) else (exc.reason,)
            raise AppServerError(("invalid_workspace_cwd", *reason)) from exc

    def _resolve_turn_sandbox_policy(self, workspace: str) -> dict[str, Any]:
        # Default sandbox gives the agent write access only to the running workspace.
        if self._coding_agent.turn_sandbox_policy is not None:
            return self._coding_agent.turn_sandbox_policy
        writable_root = canonicalize(workspace or self._workspace.root)
        return {
            "type": "workspaceWrite",
            "writableRoots": [writable_root],
            "readOnlyAccess": {"type": "fullAccess"},
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }
