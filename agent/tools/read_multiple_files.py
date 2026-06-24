from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 8000
MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MB per file


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
                # A bad path shouldn't abort the whole batch; record and continue.
                outputs.append(f"===== {path} =====\n[error: {exc}]")
                continue

            if not target.exists():
                outputs.append(f"===== {path} =====\n[error: file not found]")
                continue
            if target.is_dir():
                outputs.append(f"===== {path} =====\n[error: is a directory]")
                continue

            try:
                size = target.stat().st_size
            except OSError as exc:
                outputs.append(f"===== {path} =====\n[error: stat failed: {exc}]")
                continue
            if size > MAX_READ_BYTES:
                outputs.append(f"===== {path} =====\n[error: file too large ({size} bytes)]")
                continue

            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                outputs.append(f"===== {path} =====\n[error: read failed: {exc}]")
                continue

            original_length += len(content)
            if total_length + len(content) > MAX_OUTPUT_LENGTH and not truncated:
                remaining = MAX_OUTPUT_LENGTH - total_length
                content = content[:remaining]
                truncated = True

            outputs.append(f"===== {path} =====\n{content}")
            total_length += len(content)

            if truncated:
                # Stop reading more files once we've hit the budget.
                remaining_paths = input["paths"][input["paths"].index(path) + 1 :]
                if remaining_paths:
                    outputs.append(
                        f"[note: {len(remaining_paths)} more file(s) skipped due to "
                        "output length limit]"
                    )
                break

        metadata: dict | None = None
        if truncated:
            metadata = {"truncated": True, "original_length": original_length}

        return ToolResult(success=True, output="\n\n".join(outputs), metadata=metadata)
