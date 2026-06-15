import glob

from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000


class GlobSearchInput(BaseModel):
    pattern: str = Field(..., description="glob 文件匹配模式")


class GlobSearchTool(BaseTool):
    name = "glob_search"
    description = "按 glob 模式查找文件"
    input_schema = GlobSearchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        pattern = input["pattern"]
        prefix = self._prefix_before_wildcard(pattern)
        try:
            validate_path(prefix, ctx.workspace_path)
        except PathOutsideWorkspaceError as exc:
            return ToolResult(success=False, error=str(exc))

        try:
            matches = glob.glob(
                pattern, root_dir=str(ctx.workspace_path), recursive=True
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"Invalid glob pattern: {exc}")

        matches = sorted(matches)
        output = "\n".join(matches)
        metadata: dict | None = None
        if len(output) > MAX_OUTPUT_LENGTH:
            original_length = len(output)
            output = output[:MAX_OUTPUT_LENGTH]
            metadata = {"truncated": True, "original_length": original_length}

        return ToolResult(
            success=True, output=output or "(no matches)", metadata=metadata
        )

    def _prefix_before_wildcard(self, pattern: str) -> str:
        for i, ch in enumerate(pattern):
            if ch in "*?[":
                return pattern[:i]
        return pattern
