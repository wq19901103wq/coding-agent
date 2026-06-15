import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000


class CodeSearchInput(BaseModel):
    pattern: str = Field(..., description="要搜索的正则表达式模式")
    path: str = Field(default=".", description="相对于工作目录的搜索目录")


class CodeSearchTool(BaseTool):
    name = "code_search"
    description = "在工作目录内递归搜索代码文本"
    input_schema = CodeSearchInput

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
            compiled = re.compile(input["pattern"])
        except re.error as exc:
            return ToolResult(success=False, error=f"Invalid regex pattern: {exc}")

        matches: list[str] = []
        for root, _dirs, files in os.walk(target):
            for filename in sorted(files):
                file_path = Path(root) / filename
                try:
                    text = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

                rel = file_path.relative_to(ctx.workspace_path).as_posix()
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if compiled.search(line):
                        matches.append(f"{rel}:{lineno}: {line.rstrip()}")

        output = "\n".join(matches)
        metadata: dict | None = None
        if len(output) > MAX_OUTPUT_LENGTH:
            original_length = len(output)
            output = output[:MAX_OUTPUT_LENGTH]
            metadata = {"truncated": True, "original_length": original_length}

        return ToolResult(
            success=True, output=output or "(no matches)", metadata=metadata
        )
