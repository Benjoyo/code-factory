from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config.defaults import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT
from ..observability.api.client import ControlEndpoint
from ..observability.runtime_metadata import read_runtime_metadata
from ..workflow.loader import workflow_file_path


@dataclass(frozen=True, slots=True)
class CLIConfig:
    """Parsed CLI inputs needed to construct the long-running service."""

    workflow_path: str
    logs_root: str | None
    port: int | None


def build_cli_config(
    workflow_path: Path | None,
    logs_root: Path | None,
    port: int | None,
) -> CLIConfig:
    selected_workflow = workflow_path or Path(workflow_file_path())
    resolved_logs_root = (
        None if logs_root is None else str(logs_root.expanduser().resolve())
    )
    return CLIConfig(
        workflow_path=str(selected_workflow.expanduser().resolve()),
        logs_root=resolved_logs_root,
        port=port,
    )


def resolve_control_endpoint(
    workflow_path: Path | None, port: int | None
) -> tuple[ControlEndpoint, str]:
    resolved_workflow = build_cli_config(workflow_path, None, None).workflow_path
    if isinstance(port, int):
        return ControlEndpoint(DEFAULT_SERVER_HOST, port), resolved_workflow
    metadata = read_runtime_metadata(resolved_workflow)
    if isinstance(metadata, dict):
        host = metadata.get("host")
        bound_port = metadata.get("port")
        if isinstance(host, str) and isinstance(bound_port, int):
            return ControlEndpoint(host, bound_port), resolved_workflow
    return ControlEndpoint(DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT), resolved_workflow
