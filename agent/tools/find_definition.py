from pydantic import BaseModel, Field

from agent.indexing import Indexer
from agent.tools.base import BaseTool, ToolContext, ToolResult


class FindDefinitionInput(BaseModel):
    name: str = Field(..., description="要查找定义的符号名称")


class FindDefinitionTool(BaseTool):
    name = "find_definition"
    description = "查找符号的定义位置"
    input_schema = FindDefinitionInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        db_path = ctx.db_path or "~/.coding-agent/code_index.db"
        indexer = Indexer(ctx.workspace, db_path)
        symbols = indexer.find_definition(input["name"])

        if not symbols:
            return ToolResult(success=True, output="No definitions found.")

        lines = [f"{s.path}:{s.line}:{s.column} [{s.kind}] {s.name}" for s in symbols]
        return ToolResult(success=True, output="\n".join(lines), metadata={"count": len(symbols)})
