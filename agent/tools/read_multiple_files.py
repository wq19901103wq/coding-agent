from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 8000


class ReadMultipleFilesInput(BaseModel):
    paths: list[str] = Field(..., description="相对于工作目录的文件路径列表")


class ReadMultipleFilesTool(BaseTool):
    name = "read_multiple_files"
    description = "一次读取多个文件内容，适用于跨文件任务"
    input_schema = ReadMultipleFilesInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        outputs: list[str] = []
        total_length = 0
        truncated = False
        original_length = 0

        for path in input["paths"]:
            try:
                target = validate_path(path, ctx.workspace_path)
            except PathOutsideWorkspaceError as exc:
                return ToolResult(success=False, error=str(exc))

            if not target.exists():
                return ToolResult(success=False, error=f"File not found: {path}")
            if target.is_dir():
                return ToolResult(success=False, error=f"Is a directory: {path}")

            try:
                content = target.read_text(encoding="utf-8")
            except OSError as exc:
                return ToolResult(success=False, error=f"Failed to read {path}: {exc}")

            original_length += len(content)
            if total_length + len(content) > MAX_OUTPUT_LENGTH and not truncated:
                remaining = MAX_OUTPUT_LENGTH - total_length
                content = content[:remaining]
                truncated = True

            outputs.append(f"===== {path} =====\n{content}")
            total_length += len(content)

        metadata: dict | None = None
        if truncated:
            metadata = {"truncated": True, "original_length": original_length}

        return ToolResult(success=True, output="\n\n".join(outputs), metadata=metadata)
