from agent.tools.apply_patch import ApplyPatchTool
from agent.tools.ask_user import AskUserTool
from agent.tools.base import BaseTool, ToolContext, ToolResult
from agent.tools.code_search import CodeSearchTool
from agent.tools.execute_shell import ExecuteShellTool
from agent.tools.fetch_url import FetchUrlTool
from agent.tools.find_definition import FindDefinitionTool
from agent.tools.find_references import FindReferencesTool
from agent.tools.glob_search import GlobSearchTool
from agent.tools.list_directory import ListDirectoryTool
from agent.tools.read_file import ReadFileTool
from agent.tools.read_multiple_files import ReadMultipleFilesTool
from agent.tools.set_todo import SetTodoTool
from agent.tools.str_replace_file import StrReplaceFileTool
from agent.tools.symbol_search import SymbolSearchTool
from agent.tools.web_search import WebSearchTool
from agent.tools.write_file import WriteFileTool

TOOL_REGISTRY: dict[str, BaseTool] = {}


def register_tool(tool: BaseTool) -> None:
    TOOL_REGISTRY[tool.name] = tool


def get_tool(name: str) -> BaseTool:
    if name not in TOOL_REGISTRY:
        raise KeyError(f"Tool '{name}' not found")
    return TOOL_REGISTRY[name]


register_tool(ReadFileTool())
register_tool(WriteFileTool())
register_tool(ReadMultipleFilesTool())
register_tool(ApplyPatchTool())
register_tool(SymbolSearchTool())
register_tool(FindDefinitionTool())
register_tool(FindReferencesTool())
register_tool(StrReplaceFileTool())
register_tool(ListDirectoryTool())
register_tool(GlobSearchTool())
register_tool(CodeSearchTool())
register_tool(ExecuteShellTool())
register_tool(WebSearchTool())
register_tool(FetchUrlTool())
register_tool(AskUserTool())
register_tool(SetTodoTool())

__all__ = [
    "BaseTool",
    "ToolContext",
    "ToolResult",
    "TOOL_REGISTRY",
    "register_tool",
    "get_tool",
]
