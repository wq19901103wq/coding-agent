import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000
MAX_MATCHES = 200
MAX_READ_BYTES = 2 * 1024 * 1024  # skip files larger than 2MB

# Directories that are never useful to search and can contain tens of
# thousands of files (.git, build caches, venvs, etc.).
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".tox",
    ".eggs",
    "*.egg-info",
}


class CodeSearchInput(BaseModel):
    pattern: str = Field(..., description="要搜索的正则表达式模式")
    path: str = Field(
        default=".",
        description="相对于工作目录的搜索路径，可以是目录或单个文件",
    )
    file_pattern: str | None = Field(
        default=None,
        description="可选的文件名过滤模式（glob），例如 *.py",
    )


class CodeSearchTool(BaseTool):
    name = "code_search"
    description = "在工作目录内搜索代码文本。path 可以是目录（递归搜索）或单个文件。"
    input_schema = CodeSearchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        path = input.get("path", ".")
        file_pattern = input.get("file_pattern")
        try:
            target = validate_path(path, ctx.workspace_path)
        except PathOutsideWorkspaceError as exc:
            return ToolResult(success=False, error=str(exc))

        if not target.exists():
            return ToolResult(success=False, error=f"Path not found: {path}")

        try:
            compiled = re.compile(input["pattern"])
        except re.error as exc:
            return ToolResult(success=False, error=f"Invalid regex pattern: {exc}")

        matches: list[str] = []
        truncated = False

        if target.is_file():
            # Search a single file.
            matches, truncated = self._search_file(target, compiled, ctx.workspace_path, matches, 0)
        else:
            # Recursively search a directory.
            for root, dirs, files in os.walk(target):
                # Prune skipped directories in-place so os.walk doesn't descend.
                dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
                for filename in sorted(files):
                    if any(Path(filename).match(pat) for pat in ("*.pyc", "*.pyo", "*.so", "*.o")):
                        continue
                    if file_pattern and not Path(filename).match(file_pattern):
                        continue
                    file_path = Path(root) / filename
                    matches, truncated = self._search_file(
                        file_path, compiled, ctx.workspace_path, matches, MAX_READ_BYTES
                    )
                    if truncated:
                        break
                if truncated:
                    break

        output = "\n".join(matches)
        metadata: dict | None = None
        if truncated or len(output) > MAX_OUTPUT_LENGTH:
            original_length = len(output)
            output = output[:MAX_OUTPUT_LENGTH]
            metadata = {
                "truncated": True,
                "original_length": original_length,
                "match_count": len(matches),
            }

        return ToolResult(success=True, output=output or "(no matches)", metadata=metadata)

    def _search_file(
        self,
        file_path: Path,
        compiled: re.Pattern,
        workspace_path: Path,
        matches: list[str],
        max_bytes: int,
    ) -> tuple[list[str], bool]:
        try:
            if max_bytes and file_path.stat().st_size > max_bytes:
                return matches, False
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return matches, False

        rel = file_path.relative_to(workspace_path).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                if len(matches) >= MAX_MATCHES:
                    return matches, True
        return matches, False
