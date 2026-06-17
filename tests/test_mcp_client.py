"""MCP client 与 adapter 的单元测试（不依赖真实 MCP server）。"""

from unittest.mock import MagicMock

from agent.tools.base import ToolContext
from agent.tools.mcp_adapter import MCPToolAdapter


class _FakeMCPTool:
    def __init__(self):
        self.name = "fake"
        self.description = "fake mcp tool"
        self.inputSchema = {
            "properties": {"x": {"type": "string", "description": "input"}},
            "required": ["x"],
        }


class _FakeMCPClient:
    def __init__(self, result):
        self._result = result

    def call_tool(self, name, arguments):
        return self._result


def test_mcp_adapter_success():
    client = _FakeMCPClient({"content": [{"type": "text", "text": "ok"}]})
    adapter = MCPToolAdapter(_FakeMCPTool(), client)
    ctx = ToolContext(workspace="/tmp")

    result = adapter.execute({"x": "1"}, ctx)

    assert result.success
    assert "ok" in result.output


def test_mcp_adapter_error():
    client = _FakeMCPClient(None)
    client.call_tool = MagicMock(side_effect=RuntimeError("boom"))
    adapter = MCPToolAdapter(_FakeMCPTool(), client)
    ctx = ToolContext(workspace="/tmp")

    result = adapter.execute({"x": "1"}, ctx)

    assert not result.success
    assert "boom" in result.error
