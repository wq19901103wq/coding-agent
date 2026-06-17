"""Mock LLM client for worker subprocess testing."""

from __future__ import annotations

import json

from agent.config import LLMConfig
from agent.llm.client import LLMClient
from agent.llm.schema import AssistantResponse, ToolCall


class MockLLMClient(LLMClient):
    """LLM client that replays canned responses from a JSON file."""

    def __init__(self, responses_path: str):
        super().__init__(config=LLMConfig())
        with open(responses_path, encoding="utf-8") as f:
            data = json.load(f)
        self.responses = [self._deserialize(r) for r in data]
        self.call_count = 0

    def _deserialize(self, raw: dict) -> AssistantResponse:
        tool_calls: list[ToolCall] = []
        if raw.get("tool_calls"):
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", {}),
                )
                for tc in raw["tool_calls"]
            ]
        return AssistantResponse(
            content=raw.get("content", ""),
            tool_calls=tool_calls,
        )

    def chat(self, messages, tools=None):
        if self.call_count >= len(self.responses):
            return AssistantResponse(content="")
        response = self.responses[self.call_count]
        self.call_count += 1
        return response
