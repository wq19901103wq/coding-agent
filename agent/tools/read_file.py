from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000
# Refuse to read files larger than this in one go to avoid OOM. The agent can
# use code_search / symbol_search for targeted access to large files instead.
MAX_READ_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_LINE_LIMIT = 2000


class ReadFileInput(BaseModel):
    path: str = Field(..., description="相对于工作目录的文件路径")
    offset: int = Field(
        default=0,
        description="从第几行开始读（0-based）；用于分页读取大文件的后半部分",
    )
    limit: int = Field(
        default=DEFAULT_LINE_LIMIT,
        description="最多读取的行数；配合 offset 可分页读取大文件",
    )


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取指定文件内容，支持按行分页（offset/limit）读取大文件的任意部分。"
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
                    f"File is too large ({size} bytes; limit {MAX_READ_BYTES}). "
                    "Use code_search or symbol_search for targeted access."
                ),
            )

        offset = max(input.get("offset", 0), 0)
        limit = max(input.get("limit", DEFAULT_LINE_LIMIT), 1)

        try:
            # Stream line by line so we only hold `limit` lines in memory,
            # not the whole file. errors="replace" so binary/mixed-encoding
            # files don't raise UnicodeDecodeError (a ValueError, not OSError).
            selected: list[str] = []
            total_lines = 0
            with target.open("r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f):
                    total_lines = lineno + 1
                    if lineno < offset:
                        continue
                    if len(selected) >= limit:
                        continue
                    # Strip the trailing newline for consistent output; we
                    # re-add newlines when joining.
                    selected.append(line.rstrip("\n"))
                    # Also respect the character budget so we don't blow up
                    # the LLM context with one giant line.
                    if sum(len(s) for s in selected) >= MAX_OUTPUT_LENGTH:
                        break
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to read file: {exc}")

        if not selected:
            metadata = {
                "total_lines": total_lines,
                "offset": offset,
                "lines_returned": 0,
            }
            if offset > 0 and offset >= total_lines:
                return ToolResult(
                    success=False,
                    error=(f"offset {offset} is past end of file ({total_lines} lines)."),
                    metadata=metadata,
                )
            return ToolResult(success=True, output="(empty file)", metadata=metadata)

        # Prefix each line with its 1-based line number so the agent can
        # reference exact locations and know where it is in the file.
        numbered = [f"{offset + i + 1:>6}: {line}" for i, line in enumerate(selected)]
        output = "\n".join(numbered)

        end_line = offset + len(selected)
        has_more = end_line < total_lines
        metadata = {
            "total_lines": total_lines,
            "offset": offset,
            "lines_returned": len(selected),
            "end_line": end_line,
            "has_more": has_more,
        }
        if has_more:
            metadata["next_offset"] = end_line
        if sum(len(s) for s in selected) >= MAX_OUTPUT_LENGTH:
            metadata["truncated_by_length"] = True

        return ToolResult(success=True, output=output, metadata=metadata)
