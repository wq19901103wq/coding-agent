from typing import Any, Literal
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """LLM 请求调用的单个工具。"""

    id: str
    name: str
    arguments: dict[str, Any]


class Message(BaseModel):
    """对话消息，兼容 OpenAI Chat Completions 格式。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class Usage(BaseModel):
    """Token 使用统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AssistantResponse(BaseModel):
    """LLM 返回的助手响应。"""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)


class LLMError(Exception):
    """LLM 调用或解析过程中发生的错误。"""
