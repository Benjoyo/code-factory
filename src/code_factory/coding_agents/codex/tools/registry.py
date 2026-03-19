"""Registry and shared helpers for dynamic tools exposed to Codex."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import getdoc
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

from .results import (
    ToolExecutionError,
    ToolExecutionOutcome,
    ToolInputError,
    ToolResult,
)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Shared runtime dependencies injected into dynamic tools."""

    linear_client: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
    allowed_roots: tuple[str, ...] = ()


type ToolArguments = Any
type ToolPayload = Any
ArgsT = TypeVar("ArgsT", bound=BaseModel)
type ToolHandler[ArgsT: BaseModel] = Callable[
    [ToolContext, ArgsT], Awaitable[ToolResult]
]


@dataclass(frozen=True, slots=True)
class ToolDefinition[ArgsT: BaseModel]:
    """Declarative metadata plus runtime hooks for one dynamic tool."""

    name: str
    description: str
    args_model: type[ArgsT]
    handler: ToolHandler[ArgsT]

    def parse(self, arguments: ToolArguments) -> ArgsT:
        try:
            return self.args_model.model_validate(arguments)
        except Exception as exc:
            raise ToolInputError(_validation_error_payload(self.name, exc)) from exc

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": build_input_schema(self.args_model),
        }


def dynamic_tool[ArgsT: BaseModel](
    *,
    args_model: type[ArgsT],
    name: str | None = None,
    description: str | None = None,
) -> Callable[[ToolHandler[ArgsT]], ToolDefinition[ArgsT]]:
    """Build a registered dynamic tool definition from a handler function."""

    def decorator(handler: ToolHandler[ArgsT]) -> ToolDefinition[ArgsT]:
        tool_name = (name or handler.__name__).strip()
        tool_description = (description or getdoc(handler) or "").strip()
        if not tool_name:
            raise ValueError("dynamic_tool requires a non-empty tool name")
        if not tool_description:
            raise ValueError(
                f"dynamic_tool `{tool_name}` requires a description or docstring"
            )
        return ToolDefinition(
            name=tool_name,
            description=tool_description,
            args_model=args_model,
            handler=handler,
        )

    return decorator


class DynamicToolExecutor:
    """Executes the workspace-aware tools that Code Factory injects into Codex."""

    def __init__(
        self,
        linear_client: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        allowed_roots: tuple[str, ...] = (),
        tools: tuple[ToolDefinition[Any], ...] = (),
    ) -> None:
        self._context = ToolContext(
            linear_client=linear_client,
            allowed_roots=allowed_roots,
        )
        self._tools: tuple[ToolDefinition[Any], ...] = cast(
            tuple[ToolDefinition[Any], ...], tools or TOOLS
        )
        self._tool_map: dict[str, ToolDefinition[Any]] = {
            tool.name: tool for tool in self._tools
        }

    async def execute(
        self, tool: str | None, arguments: ToolArguments
    ) -> ToolExecutionOutcome:
        """Dispatch a tool call and return the transport-neutral result."""

        definition = self._tool_map.get(tool or "")
        if definition is None:
            return ToolExecutionOutcome(
                success=False,
                payload=unsupported_tool_payload(tool, self._tools),
                event="unsupported_tool_call",
            )
        try:
            parsed_arguments = definition.parse(arguments)
            result = await definition.handler(self._context, parsed_arguments)
            return ToolExecutionOutcome(
                success=result.success,
                payload=result.payload,
                event="tool_call_completed",
            )
        except (ToolInputError, ToolExecutionError) as exc:
            return ToolExecutionOutcome(
                success=False,
                payload=exc.payload,
                event="tool_call_completed",
            )
        except Exception:
            return ToolExecutionOutcome(
                success=False,
                payload=unexpected_tool_failure_payload(definition.name),
                event="tool_call_completed",
            )


def tool_specs() -> list[dict[str, Any]]:
    """Return the supported tool definitions in app-server schema format."""

    return [tool.spec() for tool in TOOLS]


def supported_tool_names() -> list[str]:
    """Convenience accessor used when reporting unsupported tool calls."""

    return [tool.name for tool in TOOLS]


def build_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Serialize a Pydantic model into the compact schema shape the app server expects."""

    schema = _normalize_schema(model.model_json_schema(mode="validation"))
    schema.pop("description", None)
    return schema


def unsupported_tool_payload(
    tool: str | None, tools: tuple[ToolDefinition[Any], ...]
) -> dict[str, Any]:
    return {
        "error": {
            "message": f"Unsupported dynamic tool: {tool!r}.",
            "supportedTools": [item.name for item in tools],
        }
    }


def unexpected_tool_failure_payload(tool_name: str) -> dict[str, Any]:
    return {
        "error": {
            "message": f"Dynamic tool `{tool_name}` failed unexpectedly.",
        }
    }


def _validation_error_payload(tool_name: str, reason: Exception) -> dict[str, Any]:
    if isinstance(reason, ValidationError):
        error = reason.errors()[0]
        field = ".".join(str(item) for item in error.get("loc", ()))
        message = error.get("msg", "invalid arguments")
        if error.get("type") == "extra_forbidden" and field:
            message = f"unexpected field: `{field}`"
        elif error.get("type") == "missing" and field:
            message = f"`{field}` is required"
        if tool_name not in message:
            message = f"{tool_name}: {message}"
        return {"error": {"message": message}}
    return {
        "error": {
            "message": f"`{tool_name}` received invalid input.",
            "reason": str(reason),
        }
    }


def _normalize_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"default", "title"}:
            continue
        if key == "anyOf":
            options = [_normalize_schema(option) for option in item]
            compact = _compact_nullable_object_union(options)
            if compact is not None:
                normalized.update(compact)
                continue
            normalized[key] = options
            continue
        normalized[key] = _normalize_schema(item)
    return normalized


def _compact_nullable_object_union(
    options: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if len(options) != 2:
        return None
    object_option = next(
        (
            option
            for option in options
            if isinstance(option, dict) and option.get("type") == "object"
        ),
        None,
    )
    null_option = next(
        (
            option
            for option in options
            if isinstance(option, dict) and option.get("type") == "null"
        ),
        None,
    )
    if object_option is None or null_option is None:
        return None
    return {
        "type": ["object", "null"],
        "additionalProperties": object_option.get("additionalProperties", True),
    }


from .linear_graphql import linear_graphql
from .sync_workpad import sync_workpad

TOOLS = (linear_graphql, sync_workpad)
