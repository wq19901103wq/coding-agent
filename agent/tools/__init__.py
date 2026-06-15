from agent.tools.base import BaseTool, ToolContext, ToolResult

TOOL_REGISTRY: dict[str, BaseTool] = {}


def register_tool(tool: BaseTool) -> None:
    TOOL_REGISTRY[tool.name] = tool


def get_tool(name: str) -> BaseTool:
    if name not in TOOL_REGISTRY:
        raise KeyError(f"Tool '{name}' not found")
    return TOOL_REGISTRY[name]
