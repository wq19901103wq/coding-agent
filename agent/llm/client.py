import json
import os
import time
from typing import Any

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError

from agent.config import LLMConfig

from .parser import parse_assistant_response
from .schema import AssistantResponse, LLMError, Message


class LLMClient:
    """封装 OpenAI 兼容接口的 LLM 客户端，支持重试。"""

    def __init__(self, config: LLMConfig | None = None, client: OpenAI | None = None):
        self.config = config or LLMConfig()
        self._client = client or self._build_client()

    def _build_client(self) -> OpenAI:
        api_key = self.config.api_key or os.getenv("CODING_AGENT_LLM_API_KEY", "")
        # 允许空 key 创建客户端，避免在仅启动 REPL 或运行单元测试时失败；
        # 真正的鉴权错误会在实际 API 调用时抛出。
        return OpenAI(api_key=api_key or "dummy", base_url=self.config.base_url)

    def _prepare_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """将内部 Message 列表转换为 OpenAI SDK 所需格式。"""
        result: list[dict[str, Any]] = []
        for msg in messages:
            data: dict[str, Any] = {"role": msg.role}
            if msg.content is not None:
                data["content"] = msg.content
            if msg.tool_calls:
                data["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id is not None:
                data["tool_call_id"] = msg.tool_call_id
            result.append(data)
        return result

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> AssistantResponse:
        """发送对话请求并返回解析后的响应。"""
        api_key = self.config.api_key or os.getenv("CODING_AGENT_LLM_API_KEY", "")
        if not api_key:
            raise LLMError("LLM API key is not configured")

        payload_messages = self._prepare_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": payload_messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools

        last_error: Exception | None = None
        max_attempts = self.config.max_retries_per_step + 1
        for attempt in range(max_attempts):
            try:
                response = self._client.chat.completions.create(**kwargs)
                return parse_assistant_response(response)
            except (
                APIError,
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
            ) as exc:
                last_error = exc
                if attempt < self.config.max_retries_per_step:
                    time.sleep(2**attempt)
                    continue
                break

        raise LLMError(f"LLM request failed after {max_attempts} attempts: {last_error}")
