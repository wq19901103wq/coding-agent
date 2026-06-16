from pydantic import BaseModel, Field

from agent.indexing import Indexer
from agent.tools.base import BaseTool, ToolContext, ToolResult


class FindReferencesInput(BaseModel):
    name: str = Field(..., description="要查找引用的符号名称")


class FindReferencesTool(BaseTool):
    name = "find_references"
    description = "查找符号的所有引用位置"
    input_schema = FindReferencesInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        db_path = ctx.db_path or "~/.coding-agent/code_index.db"
        indexer = Indexer(ctx.workspace, db_path)
        refs = indexer.find_references(input["name"])

        if not refs:
            return ToolResult(success=True, output="No references found.")

        lines = [
            f"{r.path}:{r.line}:{r.column} {r.name} {'(def)' if r.is_definition else ''}"
            for r in refs
        ]
        return ToolResult(success=True, output="\n".join(lines), metadata={"count": len(refs)})
