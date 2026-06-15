from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult


class SetTodoInput(BaseModel):
    action: str = Field(..., description="操作类型：create/update/complete/list")
    id: str | None = Field(default=None, description="待办事项 ID")
    title: str | None = Field(default=None, description="待办事项标题")
    status: str | None = Field(default=None, description="待办事项状态")


class SetTodoTool(BaseTool):
    name = "set_todo"
    description = "管理待办事项（创建、更新、完成、列表）"
    input_schema = SetTodoInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        action = input.get("action", "")
        todo_id = input.get("id")
        title = input.get("title")
        status = input.get("status")

        if action == "create":
            parts = ["已创建待办"]
            if todo_id:
                parts.append(f" (id={todo_id})")
            if title:
                parts.append(f"：{title}")
            return ToolResult(success=True, output="".join(parts))

        if action == "update":
            msg = f"已更新待办 {todo_id}"
            if status:
                msg += f" 状态为 [{status}]"
            return ToolResult(success=True, output=msg)

        if action == "complete":
            return ToolResult(success=True, output=f"已完成待办 {todo_id}")

        if action == "list":
            return ToolResult(
                success=True, output="待办列表：\n（当前没有持久化待办数据）"
            )

        return ToolResult(success=False, error=f"不支持的操作：{action}")
