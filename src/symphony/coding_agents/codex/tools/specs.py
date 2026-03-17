from __future__ import annotations

from typing import Any

LINEAR_GRAPHQL_TOOL = "linear_graphql"
SYNC_WORKPAD_TOOL = "sync_workpad"


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": LINEAR_GRAPHQL_TOOL,
            "description": "Execute a raw GraphQL query or mutation against Linear using Symphony's configured auth.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "GraphQL query or mutation document to execute against Linear.",
                    },
                    "variables": {
                        "type": ["object", "null"],
                        "description": "Optional GraphQL variables object.",
                        "additionalProperties": True,
                    },
                },
            },
        },
        {
            "name": SYNC_WORKPAD_TOOL,
            "description": "Create or update a workpad comment on a Linear issue. Reads the body from a local file to keep the conversation context small.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["issue_id", "file_path"],
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": 'Linear issue identifier (e.g. "ENG-123") or internal UUID.',
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Path to a local markdown file whose contents become the comment body.",
                    },
                    "comment_id": {
                        "type": "string",
                        "description": "Existing comment ID to update. Omit to create a new comment.",
                    },
                },
            },
        },
    ]


def supported_tool_names() -> list[str]:
    return [spec["name"] for spec in tool_specs()]
