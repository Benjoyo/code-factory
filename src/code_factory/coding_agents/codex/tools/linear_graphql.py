"""`linear_graphql` dynamic tool definition and execution helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from .linear_errors import linear_error_payload
from .registry import ToolContext, dynamic_tool
from .results import ToolExecutionError, ToolResult


class LinearGraphqlInput(BaseModel):
    """Advertised object shape for the `linear_graphql` tool."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"additionalProperties": False},
    )

    query: str = Field(
        description="GraphQL query or mutation document to execute against Linear."
    )
    variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional GraphQL variables object.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_arguments(cls, arguments: Any) -> Any:
        """Accept either a raw GraphQL string or a `{query, variables}` object."""

        if isinstance(arguments, str):
            query = arguments.strip()
            if not query:
                raise PydanticCustomError(
                    "linear_graphql_missing_query",
                    "`linear_graphql` requires a non-empty `query` string.",
                )
            return {"query": query, "variables": {}}
        if not isinstance(arguments, dict):
            raise PydanticCustomError(
                "linear_graphql_invalid_arguments",
                "`linear_graphql` expects either a GraphQL query string or an object with `query` and optional `variables`.",
            )
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise PydanticCustomError(
                "linear_graphql_missing_query",
                "`linear_graphql` requires a non-empty `query` string.",
            )
        variables = arguments.get("variables")
        if variables is None:
            normalized_variables: dict[str, Any] = {}
        elif not isinstance(variables, dict):
            raise PydanticCustomError(
                "linear_graphql_invalid_variables",
                "`linear_graphql.variables` must be a JSON object when provided.",
            )
        else:
            normalized_variables = variables
        normalized = dict(arguments)
        normalized["query"] = query.strip()
        normalized["variables"] = normalized_variables
        return normalized


@dynamic_tool(args_model=LinearGraphqlInput)
async def linear_graphql(
    context: ToolContext, arguments: LinearGraphqlInput
) -> ToolResult:
    """Execute a raw GraphQL query or mutation against Linear using Code Factory's configured auth."""

    try:
        response = await context.linear_client(arguments.query, arguments.variables)
    except Exception as exc:
        raise ToolExecutionError(linear_error_payload(exc)) from exc
    if (
        isinstance(response, dict)
        and isinstance(response.get("errors"), list)
        and response["errors"]
    ):
        return ToolResult.fail(response)
    return ToolResult.ok(response)
