import json
import logging
import os
import random
import time
import uuid
from collections import defaultdict
from typing import Any, Generator

import httpx
from httpx import ReadError, RemoteProtocolError
from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError

from agent.config import LLMConfig

from .parser import parse_assistant_response
from .schema import AssistantResponse, LLMError, Message, ToolCall

logger = logging.getLogger("agent.llm.client")


class LLMClient:
    """封装 OpenAI 兼容接口的 LLM 客户端，支持重试与流式输出。"""

    def __init__(self, config: LLMConfig | None = None, client: OpenAI | None = None):
        self.config = config or LLMConfig()
        self._client = client or self._build_client()

    def _build_client(self) -> OpenAI:
        api_key = self.config.api_key or os.getenv("CODING_AGENT_LLM_API_KEY", "")
        # 允许空 key 创建客户端，避免在仅启动 REPL 或运行单元测试时失败；
        # 真正的鉴权错误会在实际 API 调用时抛出。
        headers = dict(self.config.headers)
        if "api.kimi.com" in (self.config.base_url or "").lower():
            headers.setdefault("User-Agent", "KimiCLI/1.30.0")
        timeout = httpx.Timeout(
            self.config.timeout,
            connect=10.0,
            read=self.config.stream_read_timeout,
            write=60.0,
            pool=10.0,
        )
        return OpenAI(
            api_key=api_key or "dummy",
            base_url=self.config.base_url,
            default_headers=headers,
            timeout=timeout,
        )

    def _prepare_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """将内部 Message 列表转换为 OpenAI SDK 所需格式。"""
        result: list[dict[str, Any]] = []
        for msg in messages:
            data: dict[str, Any] = {"role": msg.role}
            content = msg.content
            # OpenAI 要求 assistant 消息若不带 tool_calls，则 content 不能为空
            if msg.role == "assistant" and not content and not msg.tool_calls:
                content = "（无内容）"
            if content is not None:
                data["content"] = content
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
            if msg.tool_call_id:
                data["tool_call_id"] = msg.tool_call_id
            result.append(data)

        # 调试日志：记录发送给 LLM 的消息结构，帮助定位 tool_call_id 问题
        for idx, payload in enumerate(result):
            if payload.get("tool_calls"):
                logger.debug(
                    "LLM payload[%s] assistant tool_calls: %s",
                    idx,
                    [tc.get("id") for tc in payload["tool_calls"]],
                )
            if payload.get("tool_call_id") is not None:
                logger.debug("LLM payload[%s] tool_call_id: %r", idx, payload["tool_call_id"])

        return result

    def _build_kwargs(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload_messages = self._prepare_messages(messages)
        # kimi-for-coding 只支持 temperature=1
        # deepseek v4 models work best at low temperature for code fixes
        if "deepseek" in (self.config.model or "").lower():
            effective_temperature = 0.0
        elif self.config.model == "kimi-for-coding":
            effective_temperature = 1.0
        else:
            effective_temperature = temperature
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": payload_messages,
            "temperature": effective_temperature,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools
        return kwargs

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> AssistantResponse:
        """发送非流式对话请求并返回解析后的响应。"""
        api_key = self.config.api_key or os.getenv("CODING_AGENT_LLM_API_KEY", "")
        if not api_key:
            raise LLMError("LLM API key is not configured")

        kwargs = self._build_kwargs(messages, tools, temperature, stream=False)
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
                ReadError,
                RemoteProtocolError,
            ) as exc:
                last_error = exc
                if attempt < self.config.max_retries_per_step:
                    delay = min(2**attempt + random.random(), 60)
                    time.sleep(delay)
                    continue
                break

        logger.error("LLM request failed after %s attempts: %s", max_attempts, last_error)
        raise LLMError(f"LLM request failed after {max_attempts} attempts: {last_error}")

    def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> Generator[str | AssistantResponse, None, None]:
        """发送流式对话请求。

        产生的内容：
        - str: 当前 token 文本片段
        - AssistantResponse: 流结束时产生的完整响应（包含 content 和 tool_calls）
        """
        api_key = self.config.api_key or os.getenv("CODING_AGENT_LLM_API_KEY", "")
        if not api_key:
            raise LLMError("LLM API key is not configured")

        kwargs = self._build_kwargs(messages, tools, temperature, stream=True)
        last_error: Exception | None = None
        max_attempts = self.config.max_retries_per_step + 1

        for attempt in range(max_attempts):
            try:
                stream = self._client.chat.completions.create(**kwargs)
                yield from self._parse_stream(stream)
                return
            except (
                APIError,
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                ReadError,
                RemoteProtocolError,
            ) as exc:
                last_error = exc
                if attempt < self.config.max_retries_per_step:
                    delay = min(2**attempt + random.random(), 60)
                    time.sleep(delay)
                    continue
                break

        raise LLMError(f"LLM request failed after {max_attempts} attempts: {last_error}")

    def _parse_stream(self, stream: Any) -> Generator[str | AssistantResponse, None, None]:
        """解析 OpenAI 流式响应。"""
        content_parts: list[str] = []
        # index -> {"id": ..., "name": ..., "arguments": ...}
        tool_calls: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"id": "", "name": "", "arguments": ""}
        )
        # 当 LLM 没有在流中返回 tool_call_id 时，使用稳定的 fallback id。
        fallback_ids: dict[int, str] = {}

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                yield delta.content

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    tc_id = (tc.id or "").strip()
                    if tc_id:
                        tool_calls[idx]["id"] = tc_id
                    elif not tool_calls[idx]["id"]:
                        # 首个 chunk 没有 id 时立即生成稳定 fallback，
                        # 确保同一 tool call 在所有 chunk 中使用相同 id。
                        if idx not in fallback_ids:
                            fallback_ids[idx] = f"call_{uuid.uuid4().hex[:12]}"
                        tool_calls[idx]["id"] = fallback_ids[idx]
                    if tc.function and tc.function.name:
                        tool_calls[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls[idx]["arguments"] += tc.function.arguments

        parsed_tool_calls: list[ToolCall] = []
        for idx in sorted(tool_calls.keys()):
            data = tool_calls[idx]
            try:
                arguments = json.loads(data["arguments"]) if data["arguments"] else {}
            except json.JSONDecodeError:
                arguments = {}
            parsed_tool_calls.append(
                ToolCall(
                    id=data["id"] or fallback_ids.get(idx) or f"call_{idx}",
                    name=data["name"],
                    arguments=arguments,
                )
            )

        yield AssistantResponse(
            content="".join(content_parts) if content_parts else None,
            tool_calls=parsed_tool_calls if parsed_tool_calls else [],
        )
