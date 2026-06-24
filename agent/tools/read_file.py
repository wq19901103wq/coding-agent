from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000
# Refuse to read files larger than this in one go to avoid OOM. The agent can
# use code_search / symbol_search for targeted access to large files instead.
MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MB


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

        # Guard against huge files before reading anything.
        try:
            size = target.stat().st_size
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to stat file: {exc}")
        if size > MAX_READ_BYTES:
            return ToolResult(
                success=False,
                error=(
                    f"File is too large to read at once ({size} bytes; limit "
                    f"{MAX_READ_BYTES}). Use code_search or symbol_search for "
                    "targeted access, or read it in chunks."
                ),
            )

        try:
            # errors="replace" so binary/mixed-encoding files don't crash with
            # UnicodeDecodeError (which is a ValueError, not OSError, and would
            # otherwise escape the handler).
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to read file: {exc}")

        metadata: dict | None = None
        if len(content) > MAX_OUTPUT_LENGTH:
            original_length = len(content)
            content = content[:MAX_OUTPUT_LENGTH]
            metadata = {
                "truncated": True,
                "original_length": original_length,
                "note": "Only the first %d chars are shown." % MAX_OUTPUT_LENGTH,
            }

        return ToolResult(success=True, output=content, metadata=metadata)
