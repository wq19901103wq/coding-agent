"""Guarded tool for low-friction automatic project memory."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent.memory import MemoryManager
from agent.tools.base import BaseTool, ToolContext, ToolResult


class RememberProjectMemoryInput(BaseModel):
    fact: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "A concise, stable fact useful in future sessions, such as a verified build command, "
            "architecture convention, recurring debugging insight, or user preference."
        ),
    )


class RememberProjectMemoryTool(BaseTool):
    name = "remember_project_memory"
    description = (
        "自动保存一条跨会话项目记忆。仅记录已验证、未来仍有用的稳定事实；"
        "不要记录临时任务状态、猜测、显而易见的代码内容或任何密钥。"
    )
    input_schema = RememberProjectMemoryInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        parsed = self.input_schema(**input)
        config = ctx.config.get("memory", {}) if isinstance(ctx.config, dict) else {}
        if not config.get("enabled", True):
            return ToolResult(success=False, error="项目记忆已关闭")
        if not config.get("auto_save", True):
            return ToolResult(success=False, error="自动记忆已关闭；用户仍可使用 /memory add")

        memory = MemoryManager(
            ctx.workspace,
            enabled=True,
            max_chars=int(config.get("max_chars", 25_000)),
            storage_root=str(config.get("storage_root", "~/.coding-agent/projects")),
        )
        try:
            added = memory.add(parsed.fact)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        if not added:
            return ToolResult(success=True, output="该项目记忆已存在，无需重复保存")
        return ToolResult(success=True, output="已自动保存项目记忆")
