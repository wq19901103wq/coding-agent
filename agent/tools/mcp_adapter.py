"""把 MCP server 的工具适配为 agent 内部 BaseTool。"""

from typing import Any

from pydantic import BaseModel, Field, create_model

from agent.mcp_client import MCPClient
from agent.tools.base import BaseTool, ToolContext, ToolResult


def _mcp_schema_to_pydantic(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """将 MCP JSON Schema 简单转换为 pydantic 模型。"""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for field_name, field_schema in properties.items():
        field_type = str
        default = ... if field_name in required else None
        description = field_schema.get("description", "")
        fields[field_name] = (
            field_type,
            Field(default=default, description=description),
        )
    return create_model(name, **fields)


class MCPToolAdapter(BaseTool):
    """包装单个 MCP 工具，使其能被 agent 工具注册和使用。"""

    def __init__(self, mcp_tool: Any, client: MCPClient):
        self.name = mcp_tool.name
        self.description = mcp_tool.description or ""
        self.input_schema = _mcp_schema_to_pydantic(
            f"MCPInput_{self.name}", getattr(mcp_tool, "inputSchema", {})
        )
        self._client = client

    def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = self._client.call_tool(self.name, input)
            return ToolResult(success=True, output=str(result))
        except Exception as exc:
            return ToolResult(success=False, error=f"MCP tool error: {exc}")
