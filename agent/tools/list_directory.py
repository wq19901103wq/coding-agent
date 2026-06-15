from pathlib import Path

from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000


class ListDirectoryInput(BaseModel):
    path: str = Field(default=".", description="相对于工作目录的目录路径")


class ListDirectoryTool(BaseTool):
    name = "list_directory"
    description = "列出指定目录的内容，区分文件和目录"
    input_schema = ListDirectoryInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        path = input.get("path", ".")
        try:
            target = validate_path(path, ctx.workspace_path)
        except PathOutsideWorkspaceError as exc:
            return ToolResult(success=False, error=str(exc))

        if not target.exists():
            return ToolResult(success=False, error=f"Directory not found: {path}")

        if not target.is_dir():
            return ToolResult(success=False, error=f"Not a directory: {path}")

        try:
            entries = sorted(
                target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to list directory: {exc}")

        lines = []
        for entry in entries:
            rel = entry.relative_to(ctx.workspace_path)
            marker = "dir" if entry.is_dir() else "file"
            lines.append(f"{marker}: {rel.as_posix()}")

        output = "\n".join(lines)
        metadata: dict | None = None
        if len(output) > MAX_OUTPUT_LENGTH:
            original_length = len(output)
            output = output[:MAX_OUTPUT_LENGTH]
            metadata = {"truncated": True, "original_length": original_length}

        return ToolResult(
            success=True, output=output or "(empty directory)", metadata=metadata
        )
