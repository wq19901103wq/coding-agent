from pydantic import BaseModel, Field

from agent.indexing import Indexer
from agent.tools.base import BaseTool, ToolContext, ToolResult


class SymbolSearchInput(BaseModel):
    query: str = Field(..., description="符号名称或名称片段")
    kind: str | None = Field(default=None, description="可选类型过滤：function/class/method")


class SymbolSearchTool(BaseTool):
    name = "symbol_search"
    description = "按名称搜索代码符号（函数、类、方法）"
    input_schema = SymbolSearchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        db_path = ctx.db_path or "~/.coding-agent/code_index.db"
        indexer = Indexer(ctx.workspace, db_path)
        symbols = indexer.search_symbols(input["query"], input.get("kind"))

        if not symbols:
            return ToolResult(success=True, output="No symbols found.")

        lines = [f"{s.path}:{s.line}:{s.column} [{s.kind}] {s.name}" for s in symbols]
        return ToolResult(success=True, output="\n".join(lines), metadata={"count": len(symbols)})
