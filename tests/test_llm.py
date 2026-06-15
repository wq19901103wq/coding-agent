import json
from typing import Any

import pytest
from openai import APIError, APITimeoutError
from pydantic import BaseModel as PydanticModel, ValidationError

from agent.config import LLMConfig
from agent.llm import (
    AssistantResponse,
    LLMClient,
    LLMError,
    Message,
    ToolCall,
    build_tool_schema,
    build_tools_payload,
    parse_assistant_response,
)
from agent.tools.base import BaseTool, ToolContext, ToolResult


class DummyInput(PydanticModel):
    x: int


class DummyTool(BaseTool):
    name = "dummy"
    description = "dummy tool"
    input_schema = DummyInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(success=True, output=str(input["x"] * 2))


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_message_roles():
    m = Message(role="user", content="hello")
    assert m.role == "user" and m.content == "hello"


def test_message_invalid_role():
    with pytest.raises(ValidationError):
        Message(role="invalid", content="hello")


def test_tool_call_arguments_roundtrip():
    tc = ToolCall(id="call_1", name="dummy", arguments={"x": 1})
    assert tc.id == "call_1"
    assert tc.arguments == {"x": 1}


def test_assistant_response_defaults():
    r = AssistantResponse(content="hi")
    assert r.content == "hi"
    assert r.tool_calls == []
    assert r.usage.total_tokens == 0


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

def test_build_tool_schema():
    schema = build_tool_schema(DummyTool())
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "dummy"
    assert "x" in schema["function"]["parameters"]["properties"]


def test_build_tools_payload():
    payload = build_tools_payload([DummyTool()])
    assert len(payload) == 1
    assert payload[0]["function"]["name"] == "dummy"


def test_parse_assistant_response_text_only():
    response = _make_response(content="hello")
    parsed = parse_assistant_response(response)
    assert parsed.content == "hello"
    assert parsed.tool_calls == []


def test_parse_assistant_response_with_tool_calls():
    response = _make_response(
        content=None,
        tool_calls=[
            _make_raw_tool_call("call_1", "dummy", '{"x": 1}'),
        ],
    )
    parsed = parse_assistant_response(response)
    assert parsed.content is None
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].id == "call_1"
    assert parsed.tool_calls[0].name == "dummy"
    assert parsed.tool_calls[0].arguments == {"x": 1}


def test_parse_assistant_response_invalid_json():
    response = _make_response(
        tool_calls=[_make_raw_tool_call("call_1", "dummy", "not json")],
    )
    with pytest.raises(ValueError, match="invalid tool call arguments JSON"):
        parse_assistant_response(response)


def test_parse_assistant_response_no_choices():
    response = _make_response(content="x", choices=[])
    with pytest.raises(ValueError, match="no choices"):
        parse_assistant_response(response)


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------

def test_client_missing_api_key():
    config = LLMConfig(api_key="")
    client = LLMClient(config=config, client=_FakeOpenAIClient())
    with pytest.raises(LLMError, match="API key is not configured"):
        client.chat([Message(role="user", content="hi")])


def test_client_successful_chat():
    fake = _FakeOpenAIClient(responses=[_make_response(content="hello")])
    config = LLMConfig(api_key="test-key")
    client = LLMClient(config=config, client=fake)

    response = client.chat([Message(role="user", content="hi")])
    assert response.content == "hello"
    assert fake.call_count == 1


def test_client_sends_tool_payload():
    fake = _FakeOpenAIClient(responses=[_make_response(content="ok")])
    config = LLMConfig(api_key="test-key")
    client = LLMClient(config=config, client=fake)

    tools = build_tools_payload([DummyTool()])
    client.chat([Message(role="user", content="hi")], tools=tools)

    assert fake.last_kwargs["tools"] == tools


def test_client_retry_then_success():
    fake = _FakeOpenAIClient(
        responses=[
            _APIError("timeout"),
            _make_response(content="recovered"),
        ]
    )
    config = LLMConfig(api_key="test-key", max_retries_per_step=2)
    client = LLMClient(config=config, client=fake)

    response = client.chat([Message(role="user", content="hi")])
    assert response.content == "recovered"
    assert fake.call_count == 2


def test_client_retry_exhausted():
    fake = _FakeOpenAIClient(responses=[_APIError("timeout")] * 4)
    config = LLMConfig(api_key="test-key", max_retries_per_step=2)
    client = LLMClient(config=config, client=fake)

    with pytest.raises(LLMError, match="failed after 3 attempts"):
        client.chat([Message(role="user", content="hi")])
    assert fake.call_count == 3


def test_client_prepares_tool_messages():
    fake = _FakeOpenAIClient(responses=[_make_response(content="ok")])
    config = LLMConfig(api_key="test-key")
    client = LLMClient(config=config, client=fake)

    messages = [
        Message(role="assistant", content=None, tool_calls=[
            ToolCall(id="call_1", name="dummy", arguments={"x": 1})
        ]),
        Message(role="tool", content="2", tool_call_id="call_1"),
    ]
    client.chat(messages)

    sent = fake.last_kwargs["messages"]
    assert sent[0]["role"] == "assistant"
    assert "tool_calls" in sent[0]
    assert sent[1]["role"] == "tool"
    assert sent[1]["tool_call_id"] == "call_1"
    assert sent[1]["content"] == "2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockMessage:
    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None):
        self.content = content
        self.tool_calls = tool_calls or []


class _MockChoice:
    def __init__(self, message):
        self.message = message


class _MockUsage:
    def __init__(self, prompt=1, completion=2, total=3):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _MockResponse:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


def _make_raw_tool_call(call_id: str, name: str, arguments: str):
    class _RawFunction:
        def __init__(self):
            self.name = name
            self.arguments = arguments

    class _RawToolCall:
        def __init__(self):
            self.id = call_id
            self.function = _RawFunction()

    return _RawToolCall()


def _make_response(
    content: str | None = "hello",
    tool_calls: list[Any] | None = None,
    choices: list[Any] | None = None,
    usage: Any = None,
):
    if choices is None:
        choices = [_MockChoice(_MockMessage(content=content, tool_calls=tool_calls))]
    return _MockResponse(choices=choices, usage=usage)


class _APIError(APIError):
    def __init__(self, message: str):
        super().__init__(message, request=None, body=None)


class _FakeOpenAIClient:
    def __init__(self, responses=None):
        self.responses = responses or []
        self.call_count = 0
        self.last_kwargs: dict[str, Any] = {}

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        response = self.responses[self.call_count]
        self.call_count += 1
        if isinstance(response, Exception):
            raise response
        return response
