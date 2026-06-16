from pathlib import Path

from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult


class ApplyPatchInput(BaseModel):
    diff: str = Field(..., description="unified diff 格式的补丁文本")


class Hunk:
    def __init__(
        self,
        old_start: int,
        old_count: int,
        new_start: int,
        new_count: int,
        lines: list[str],
    ):
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.lines = lines


class FilePatch:
    def __init__(self, old_path: str | None, new_path: str | None, hunks: list[Hunk]):
        self.old_path = old_path
        self.new_path = new_path
        self.hunks = hunks


def _parse_range(range_str: str) -> tuple[int, int]:
    body = range_str[1:]
    if "," in body:
        start, count = body.split(",", 1)
    else:
        start = body
        count = "1"
    return int(start), int(count)


def parse_diff(diff: str) -> list[FilePatch]:
    """简易 unified diff 解析器。"""
    patches: list[FilePatch] = []
    current_old: str | None = None
    current_new: str | None = None
    current_hunks: list[Hunk] = []

    hunk_old_start: int | None = None
    hunk_old_count: int | None = None
    hunk_new_start: int | None = None
    hunk_new_count: int | None = None
    hunk_lines: list[str] = []

    def flush_hunk() -> None:
        nonlocal hunk_old_start, hunk_old_count, hunk_new_start, hunk_new_count
        if hunk_old_start is None:
            return
        current_hunks.append(
            Hunk(
                hunk_old_start,
                hunk_old_count or 0,
                hunk_new_start or 0,
                hunk_new_count or 0,
                list(hunk_lines),
            )
        )
        hunk_old_start = None
        hunk_old_count = None
        hunk_new_start = None
        hunk_new_count = None
        hunk_lines.clear()

    def flush_patch() -> None:
        nonlocal current_old, current_new
        if current_old is None and current_new is None:
            return
        patches.append(FilePatch(current_old, current_new, list(current_hunks)))
        current_hunks.clear()

    for raw_line in diff.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        if line.startswith("--- "):
            flush_hunk()
            flush_patch()
            current_old = line[4:].strip()
            if current_old == "/dev/null":
                current_old = None
            current_new = None
        elif line.startswith("+++ "):
            current_new = line[4:].strip()
            if current_new == "/dev/null":
                current_new = None
        elif line.startswith("@@"):
            flush_hunk()
            parts = line.split("@@")
            ranges = parts[1].strip()
            old_range, new_range = ranges.split(" ", 1)
            hunk_old_start, hunk_old_count = _parse_range(old_range)
            hunk_new_start, hunk_new_count = _parse_range(new_range)
            hunk_lines = []
        elif hunk_old_start is not None:
            hunk_lines.append(line)

    flush_hunk()
    flush_patch()
    return patches


def apply_hunks(content_lines: list[str], hunks: list[Hunk]) -> list[str]:
    """将 hunks 应用到内容行，任一失败抛异常。"""
    result = list(content_lines)
    offset = 0
    for hunk in hunks:
        start_idx = hunk.old_start - 1 + offset
        if start_idx < 0 or start_idx > len(result):
            raise ValueError(f"Hunk start out of range: {hunk.old_start}")

        old_lines: list[str] = []
        new_lines: list[str] = []
        for line in hunk.lines:
            if line.startswith("-"):
                old_lines.append(line[1:])
            elif line.startswith("+"):
                new_lines.append(line[1:])
            elif line.startswith(" "):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
            elif line.startswith("\\"):
                # "\ No newline at end of file" 忽略
                continue

        actual = result[start_idx : start_idx + len(old_lines)]
        if actual != old_lines:
            expected = "\n".join(old_lines)
            actual_text = "\n".join(actual)
            raise ValueError(
                f"Hunk does not match at line {hunk.old_start}.\n"
                f"Expected:\n{expected}\nActual:\n{actual_text}"
            )

        result[start_idx : start_idx + len(old_lines)] = new_lines
        offset += len(new_lines) - len(old_lines)

    return result


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = "使用 unified diff 同时修改多个文件，支持新增和删除文件"
    input_schema = ApplyPatchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        diff = input["diff"]
        try:
            patches = parse_diff(diff)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse diff: {exc}")

        if not patches:
            return ToolResult(success=False, error="No file patches found in diff")

        # 第一阶段：校验所有路径合法性
        for patch in patches:
            path: str
            if patch.new_path is not None:
                path = patch.new_path
            elif patch.old_path is not None:
                path = patch.old_path
            else:
                return ToolResult(success=False, error="Patch missing both old and new path")
            try:
                validate_path(path, ctx.workspace_path)
            except PathOutsideWorkspaceError as exc:
                return ToolResult(success=False, error=str(exc))

        # 第二阶段：先全部验证通过后再写入；记录备份用于回滚
        backups: dict[Path, str | None] = {}
        try:
            for patch in patches:
                if patch.new_path is not None:
                    path = patch.new_path
                elif patch.old_path is not None:
                    path = patch.old_path
                else:
                    return ToolResult(success=False, error="Patch missing both old and new path")
                target = validate_path(path, ctx.workspace_path)

                if patch.new_path is None:
                    # 删除文件
                    backups[target] = (
                        target.read_text(encoding="utf-8") if target.exists() else None
                    )
                    if target.exists():
                        target.unlink()
                elif patch.old_path is None or not target.exists():
                    # 新增文件：收集所有 + 行
                    backups[target] = None
                    new_lines: list[str] = []
                    for hunk in patch.hunks:
                        for line in hunk.lines:
                            if line.startswith("+"):
                                new_lines.append(line[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                else:
                    # 修改文件
                    original = target.read_text(encoding="utf-8")
                    backups[target] = original
                    content_lines = original.splitlines()
                    new_lines = apply_hunks(content_lines, patch.hunks)
                    # 保持末尾换行
                    ending = "\n" if original.endswith("\n") else ""
                    target.write_text("\n".join(new_lines) + ending, encoding="utf-8")
        except Exception as exc:
            # 回滚
            for target, backup in backups.items():
                if backup is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.write_text(backup, encoding="utf-8")
            return ToolResult(success=False, error=f"Failed to apply patch: {exc}")

        affected: list[str] = []
        for patch in patches:
            if patch.new_path is not None:
                affected.append(patch.new_path)
            elif patch.old_path is not None:
                affected.append(patch.old_path)
        return ToolResult(
            success=True,
            output=f"Patch applied to {len(affected)} file(s): {', '.join(affected)}",
            metadata={"affected_files": affected},
        )
