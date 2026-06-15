from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000


class ReadFileInput(BaseModel):
    path: str = Field(..., description="相对于工作目录的文件路径")


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取指定文件内容"
    input_schema = ReadFileInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        try:
            target = validate_path(input["path"], ctx.workspace_path)
        except PathOutsideWorkspaceError as exc:
            return ToolResult(success=False, error=str(exc))

        if not target.exists():
            return ToolResult(success=False, error=f"File not found: {input['path']}")

        if target.is_dir():
            return ToolResult(success=False, error=f"Is a directory: {input['path']}")

        try:
            content = target.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to read file: {exc}")

        metadata: dict | None = None
        if len(content) > MAX_OUTPUT_LENGTH:
            original_length = len(content)
            content = content[:MAX_OUTPUT_LENGTH]
            metadata = {"truncated": True, "original_length": original_length}

        return ToolResult(success=True, output=content, metadata=metadata)
