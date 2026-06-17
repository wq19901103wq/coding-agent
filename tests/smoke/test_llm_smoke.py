"""真实 LLM 冒烟测试。

默认跳过；设置环境变量 CODING_AGENT_RUN_SMOKE_TESTS=1 后运行。
测试会消耗真实 token，请谨慎执行。
"""

import os

import pytest

from agent.config import Config, LLMConfig
from agent.llm import LLMClient, build_tools_payload
from agent.llm.schema import Message
from agent.tools.read_file import ReadFileTool

pytestmark = pytest.mark.skipif(
    os.getenv("CODING_AGENT_RUN_SMOKE_TESTS") != "1",
    reason="Set CODING_AGENT_RUN_SMOKE_TESTS=1 to run real LLM smoke tests",
)


def _load_config() -> Config:
    return Config(
        llm=LLMConfig(
            api_key=os.getenv("CODING_AGENT_LLM_API_KEY", ""),
            base_url=os.getenv("CODING_AGENT_LLM_BASE_URL", "https://api.kimi.com/coding/v1"),
            model=os.getenv("CODING_AGENT_LLM_MODEL", "kimi-for-coding"),
        )
    )


def test_real_llm_chat_response():
    config = _load_config()
    client = LLMClient(config.llm)
    response = client.chat([Message(role="user", content="Reply with exactly 'pong'.")])
    assert response.content
    assert "pong" in response.content.lower()


def test_real_llm_chat_stream():
    config = _load_config()
    client = LLMClient(config.llm)
    items = list(client.chat_stream([Message(role="user", content="Reply with exactly 'pong'.")]))
    final = items[-1]
    assert hasattr(final, "content")
    assert "pong" in (final.content or "").lower()


def test_real_llm_tool_call(tmp_path):
    config = _load_config()
    client = LLMClient(config.llm)
    (tmp_path / "hello.txt").write_text("world", encoding="utf-8")

    tools = build_tools_payload([ReadFileTool()])
    response = client.chat(
        [
            Message(
                role="system",
                content="You are a helpful assistant. Use tools when needed.",
            ),
            Message(
                role="user",
                content=f"Read {tmp_path}/hello.txt and reply with its content.",
            ),
        ],
        tools=tools,
    )
    assert response.tool_calls or response.content
