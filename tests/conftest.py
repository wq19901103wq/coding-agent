from typing import Any, Iterator

import pytest

from agent.llm.schema import AssistantResponse, Message


class MockLLM:
    """用于 REPL 测试的 mock LLM 客户端。"""

    def __init__(
        self,
        responses: list[AssistantResponse] | None = None,
        side_effect: Any = None,
    ):
        self.responses = responses or []
        self.side_effect = side_effect
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> AssistantResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if self.side_effect is not None:
            result: AssistantResponse = self.side_effect(messages, tools)
            return result
        response = self.responses[self.call_count]
        self.call_count += 1
        return response

    def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> Iterator[str | AssistantResponse]:
        """Mock 流式输出：将非流式响应拆成字符逐个返回。"""
        response = self.chat(messages, tools, temperature)
        if response.content:
            for char in response.content:
                yield char
        yield response


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """提供一个隔离的 HOME 目录，并将当前工作目录切换到该目录。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def mock_llm():
    """返回 MockLLM 类，供测试复用以构造 mock LLM 客户端。"""
    return MockLLM
