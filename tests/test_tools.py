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


def test_tool_result_success():
    r = ToolResult(success=True, output="hello")
    assert r.success and r.output == "hello"


def test_tool_registry():
    from agent.tools import TOOL_REGISTRY, register_tool

    register_tool(DummyTool())
    assert "dummy" in TOOL_REGISTRY
