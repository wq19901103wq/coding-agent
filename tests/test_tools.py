import pytest
from pydantic import BaseModel as PydanticModel

from agent.tools.base import BaseTool, ToolResult, ToolContext


class DummyInput(PydanticModel):
    x: int


class DummyTool(BaseTool):
    name = "dummy"
    description = "dummy tool"
    input_schema = DummyInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(success=True, output=str(input["x"] * 2))


@pytest.fixture
def isolated_registry(monkeypatch):
    """提供一个被清空并恢复的独立工具注册表副本。"""
    from agent.tools import TOOL_REGISTRY

    original = TOOL_REGISTRY.copy()
    TOOL_REGISTRY.clear()
    yield TOOL_REGISTRY
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(original)


def test_tool_result_success():
    r = ToolResult(success=True, output="hello")
    assert r.success and r.output == "hello"


def test_tool_registry(isolated_registry):
    from agent.tools import register_tool

    register_tool(DummyTool())
    assert "dummy" in isolated_registry


def test_get_tool_found(isolated_registry):
    from agent.tools import get_tool, register_tool

    tool = DummyTool()
    register_tool(tool)
    assert get_tool("dummy") is tool


def test_get_tool_not_found(isolated_registry):
    from agent.tools import get_tool

    with pytest.raises(KeyError, match="Tool 'missing' not found"):
        get_tool("missing")


def test_base_tool_is_abstract():
    with pytest.raises(TypeError):
        BaseTool()
