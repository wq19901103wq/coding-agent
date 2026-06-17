import json
import uuid
from typing import Any

from agent.tools.base import BaseTool

from .schema import AssistantResponse, ToolCall, Usage


def build_tool_schema(tool: BaseTool) -> dict[str, Any]:
    """将 BaseTool 转换为 OpenAI function tool schema。"""
    schema = tool.input_schema.model_json_schema()
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": schema,
        },
    }


def build_tools_payload(tools: list[BaseTool]) -> list[dict[str, Any]]:
    """为多个工具生成 OpenAI tools 参数。"""
    return [build_tool_schema(tool) for tool in tools]


def _parse_tool_call(raw: Any, fallback_id: str | None = None) -> ToolCall:
    """从 OpenAI tool_calls 项解析 ToolCall。"""
    function = raw.function
    arguments_str = function.arguments or "{}"
    try:
        arguments = json.loads(arguments_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid tool call arguments JSON: {exc}") from exc
    call_id = raw.id or fallback_id or f"call_{uuid.uuid4().hex[:12]}"
    return ToolCall(
        id=call_id,
        name=function.name,
        arguments=arguments,
    )


def parse_assistant_response(response: Any) -> AssistantResponse:
    """将 OpenAI ChatCompletion 解析为 AssistantResponse。"""
    if not response.choices:
        raise ValueError("LLM response has no choices")

    message = response.choices[0].message
    content = message.content

    tool_calls: list[ToolCall] = []
    raw_tool_calls = getattr(message, "tool_calls", None)
    if raw_tool_calls:
        for idx, raw in enumerate(raw_tool_calls):
            tool_calls.append(_parse_tool_call(raw, fallback_id=f"call_{idx}"))

    usage = Usage()
    raw_usage = getattr(response, "usage", None)
    if raw_usage:
        usage = Usage(
            prompt_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(raw_usage, "total_tokens", 0) or 0,
        )

    return AssistantResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
    )
