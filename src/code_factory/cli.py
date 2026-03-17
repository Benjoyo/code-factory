from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from .application import CodeFactoryService

ACK_FLAG = "--i-understand-that-this-will-be-running-without-the-usual-guardrails"


@dataclass(frozen=True, slots=True)
class CLIConfig:
    """Parsed CLI inputs needed to construct the long-running service."""

    workflow_path: str
    logs_root: str | None
    port: int | None


def main(argv: list[str] | None = None) -> int:
    """Validates CLI arguments and hands control to the async service loop."""

    argv = list(sys.argv[1:] if argv is None else argv)
    result = evaluate(argv)
    if isinstance(result, str):
        print(result, file=sys.stderr)
        return 1

    if not os.path.isfile(result.workflow_path):
        print(f"Workflow file not found: {result.workflow_path}", file=sys.stderr)
        return 1

    try:
        asyncio.run(
            CodeFactoryService(
                result.workflow_path,
                logs_root=result.logs_root,
                port_override=result.port,
            ).run_forever()
        )
    except KeyboardInterrupt:
        return 130
    return 0


def evaluate(args: list[str]) -> CLIConfig | str:
    """Parses the minimal CLI surface without introducing an argparse dependency."""

    workflow_path: str | None = None
    logs_root: str | None = None
    port: int | None = None
    acknowledged = False

    index = 0
    while index < len(args):
        arg = args[index]
        if arg == ACK_FLAG:
            acknowledged = True
            index += 1
            continue
        if arg == "--logs-root":
            index += 1
            if index >= len(args) or not args[index].strip():
                return usage_message()
            logs_root = os.path.abspath(os.path.expanduser(args[index]))
            index += 1
            continue
        if arg == "--port":
            index += 1
            if index >= len(args):
                return usage_message()
            try:
                port = int(args[index])
            except ValueError:
                return usage_message()
            if port < 0:
                return usage_message()
            index += 1
            continue
        if arg.startswith("-"):
            return usage_message()
        if workflow_path is not None:
            return usage_message()
        workflow_path = os.path.abspath(os.path.expanduser(arg))
        index += 1

    if not acknowledged:
        return acknowledgement_banner()

    return CLIConfig(
        workflow_path=os.path.abspath(
            os.path.expanduser(workflow_path or "WORKFLOW.md")
        ),
        logs_root=logs_root,
        port=port,
    )


def usage_message() -> str:
    """Returns the compact usage text shared by all validation failures."""

    return (
        "Usage: code-factory [--logs-root <path>] [--port <port>] [path-to-WORKFLOW.md]"
    )


def acknowledgement_banner() -> str:
    """Builds the explicit opt-in banner for preview-mode execution."""

    lines = [
        "This Code Factory implementation is a low key engineering preview.",
        "The coding agent will run without any guardrails.",
        "Code Factory is not a supported product and is presented as-is.",
        f"To proceed, start with `{ACK_FLAG}` CLI argument",
    ]
    width = max(len(line) for line in lines)
    border = "─" * (width + 2)
    content = ["╭" + border + "╮", "│ " + (" " * width) + " │"]
    content.extend(f"│ {line.ljust(width)} │" for line in lines)
    content.extend(["│ " + (" " * width) + " │", "╰" + border + "╯"])
    return "\n".join(content)
