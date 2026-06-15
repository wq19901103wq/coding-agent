from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult


class StrReplaceFileInput(BaseModel):
    path: str = Field(..., description="相对于工作目录的文件路径")
    old_str: str = Field(..., description="要替换的原字符串")
    new_str: str = Field(..., description="用于替换的新字符串")


class StrReplaceFileTool(BaseTool):
    name = "str_replace_file"
    description = "局部替换文件内容"
    input_schema = StrReplaceFileInput

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

        occurrences = content.count(input["old_str"])
        if occurrences == 0:
            return ToolResult(
                success=False,
                error=f"No match found for old_str in {input['path']}",
            )
        if occurrences > 1:
            return ToolResult(
                success=False,
                error=(
                    f"old_str must be unique in {input['path']}; "
                    f"found {occurrences} occurrences"
                ),
            )

        new_content = content.replace(input["old_str"], input["new_str"], 1)

        try:
            target.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to write file: {exc}")

        return ToolResult(
            success=True,
            output=f"File updated: {input['path']}",
            metadata={"path": str(target), "replacements": 1},
        )
