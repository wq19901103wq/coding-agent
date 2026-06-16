from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult

if TYPE_CHECKING:
    from agent.history import HistoryManager


class SetTodoInput(BaseModel):
    action: Literal["create", "update", "complete", "list"]
    id: str | None = Field(default=None, description="待办事项 ID")
    title: str | None = Field(default=None, description="待办事项标题")
    status: Literal["pending", "in_progress", "done"] | None = Field(
        default=None, description="待办事项状态"
    )


class SetTodoTool(BaseTool):
    name = "set_todo"
    description = "管理待办事项（创建、更新、完成、列表）"
    input_schema = SetTodoInput

    def _history_manager(self, ctx: ToolContext) -> HistoryManager:
        from agent.history import HistoryManager

        return HistoryManager(ctx.db_path)

    def _session_id(self, mgr: HistoryManager, ctx: ToolContext) -> str:
        return mgr.get_or_create_session(ctx.workspace)

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        self.input_schema(**input)

        action = input.get("action", "")
        todo_id = input.get("id")
        title = input.get("title")
        status = input.get("status")

        mgr = self._history_manager(ctx)
        session_id = self._session_id(mgr, ctx)

        if action == "create":
            created_id = mgr.create_todo(session_id, title or "", todo_id=todo_id)
            parts = ["已创建待办"]
            parts.append(f" (id={created_id})")
            if title:
                parts.append(f"：{title}")
            return ToolResult(success=True, output="".join(parts))

        if action == "update":
            if todo_id is None:
                return ToolResult(success=False, error="更新待办需要提供 id")
            mgr.update_todo(todo_id, title=title, status=status)
            msg = f"已更新待办 {todo_id}"
            if status:
                msg += f" 状态为 [{status}]"
            return ToolResult(success=True, output=msg)

        if action == "complete":
            if todo_id is None:
                return ToolResult(success=False, error="完成待办需要提供 id")
            mgr.complete_todo(todo_id)
            return ToolResult(success=True, output=f"已完成待办 {todo_id}")

        if action == "list":
            todos = mgr.list_todos(session_id)
            if not todos:
                return ToolResult(success=True, output="待办列表：\n（暂无待办事项）")
            lines = ["待办列表："]
            for todo in todos:
                lines.append(f"- [{todo['status']}] {todo['title']} (id={todo['id']})")
            return ToolResult(success=True, output="\n".join(lines))

        return ToolResult(success=False, error=f"不支持的操作：{action}")
