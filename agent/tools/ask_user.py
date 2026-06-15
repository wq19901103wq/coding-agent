from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult


class AskUserInput(BaseModel):
    question: str = Field(..., description="要问用户的问题")
    options: list[str] | None = Field(default=None, description="可选的选项列表")


class AskUserTool(BaseTool):
    name = "ask_user"
    description = "向用户提问并等待回复，支持提供选项列表"
    input_schema = AskUserInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        question = input.get("question", "")
        options = input.get("options")

        lines = [f"请问：{question}"]
        if options:
            lines.append("请选择一个选项并回复对应编号：")
            for idx, option in enumerate(options, start=1):
                lines.append(f"{idx}. {option}")
        else:
            lines.append("请直接回复你的答案。")

        return ToolResult(success=True, output="\n".join(lines))
