import json
from typing import Any, Literal

from anthropic.types import ToolResultBlockParam
from mcp.types import CallToolResult, ImageContent, TextContent

from mcp_client import MCPClient


class ToolManager:
    @classmethod
    async def get_all_tools(
        cls, clients: dict[str, MCPClient]
    ) -> list[dict[str, Any]]:
        """Gets all tools from the provided clients.

        Returns Anthropic tool-schema dicts, not the MCP `Tool` models they are
        built from — this is the bridge between the two, and `Chat` concatenates
        the result straight onto `local_tools.TOOLS`.
        """
        tools = []
        for client in clients.values():
            tool_models = await client.list_tools()
            tools += [
                {
                    "name": t.name,
                    "description": t.description,
                    # mcp 2.0 renamed the model fields to snake_case
                    # (`inputSchema` -> `input_schema`, `isError` ->
                    # `is_error`, `mimeType` -> `mime_type` below). The
                    # camelCase spellings survive as serialization aliases, so
                    # constructing still works either way — but attribute
                    # *reads* like these do not, and fail at runtime rather
                    # than at import.
                    "input_schema": t.input_schema,
                }
                for t in tool_models
            ]
        return tools

    @classmethod
    async def _tool_owners(
        cls, clients: dict[str, MCPClient]
    ) -> dict[str, MCPClient]:
        """Maps tool name -> the first client offering it (one list_tools per
        client, rather than one per client per tool being executed)."""
        owners: dict[str, MCPClient] = {}
        for client in clients.values():
            for tool in await client.list_tools():
                owners.setdefault(tool.name, client)
        return owners

    @classmethod
    def _build_tool_result_part(
        cls,
        tool_use_id: str,
        text: str,
        status: Literal["success", "error"],
    ) -> ToolResultBlockParam:
        """Builds a tool result part dictionary."""
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": text,
            "is_error": status == "error",
        }

    @classmethod
    def _build_tool_result_part_content(
        cls,
        tool_use_id: str,
        content,
        status: Literal["success", "error"],
    ) -> ToolResultBlockParam:
        """Like _build_tool_result_part, but accepts pre-built content
        (a string, or a list of content blocks e.g. text + image)."""
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": content,
            "is_error": status == "error",
        }

    @classmethod
    async def execute_blocks(
        cls, clients: dict[str, MCPClient], tool_use_blocks
    ) -> list[ToolResultBlockParam]:
        """Executes a list of tool_use blocks against the provided clients."""
        tool_result_blocks: list[ToolResultBlockParam] = []
        owners = await cls._tool_owners(clients)

        for tool_request in tool_use_blocks:
            tool_use_id = tool_request.id
            tool_name = tool_request.name
            tool_input = tool_request.input

            client = owners.get(tool_name)

            if not client:
                tool_result_blocks.append(
                    cls._build_tool_result_part(
                        tool_use_id, "Could not find that tool", "error"
                    )
                )
                continue

            try:
                tool_output: CallToolResult | None = await client.call_tool(
                    tool_name, tool_input
                )
                items = tool_output.content if tool_output else []
                text_list = [
                    item.text for item in items if isinstance(item, TextContent)
                ]
                image_items = [
                    item for item in items if isinstance(item, ImageContent)
                ]
                content_json = json.dumps(text_list)
                status: Literal["success", "error"] = (
                    "error"
                    if tool_output and tool_output.is_error
                    else "success"
                )

                if image_items:
                    # Forward images as real image content blocks (base64,
                    # e.g. from manage_camera's include_image=true) instead
                    # of silently dropping them and keeping only the text.
                    content = [{"type": "text", "text": content_json}] + [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": item.mime_type,
                                "data": item.data,
                            },
                        }
                        for item in image_items
                    ]
                    tool_result_blocks.append(
                        cls._build_tool_result_part_content(
                            tool_use_id, content, status
                        )
                    )
                else:
                    tool_result_blocks.append(
                        cls._build_tool_result_part(
                            tool_use_id, content_json, status
                        )
                    )
            except Exception as e:
                error_message = f"Error executing tool '{tool_name}': {e}"
                print(error_message)
                tool_result_blocks.append(
                    cls._build_tool_result_part(
                        tool_use_id,
                        json.dumps({"error": error_message}),
                        "error",
                    )
                )
        return tool_result_blocks
