from pathlib import Path

from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult


class WriteFileInput(BaseModel):
    path: str = Field(..., description="相对于工作目录的文件路径")
    content: str = Field(..., description="要写入的文件内容")
    append: bool = Field(False, description="是否以追加模式写入")


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "创建或覆盖文件"
    input_schema = WriteFileInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        try:
            target = validate_path(input["path"], ctx.workspace_path)
        except PathOutsideWorkspaceError as exc:
            return ToolResult(success=False, error=str(exc))

        if target.exists() and target.is_dir():
            return ToolResult(success=False, error=f"Is a directory: {input['path']}")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if input.get("append", False) else "w"
            with target.open(mode, encoding="utf-8") as f:
                f.write(input["content"])
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to write file: {exc}")

        action = "appended to" if input.get("append", False) else "written"
        return ToolResult(
            success=True,
            output=f"File {action}: {input['path']}",
            metadata={"path": str(target), "size": target.stat().st_size},
        )
