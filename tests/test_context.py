from agent.config import ContextConfig
from agent.context import ContextManager
from agent.llm.schema import AssistantResponse, Message
from tests.conftest import MockLLM


def test_estimate_tokens_basic():
    messages = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="hello"),
    ]
    manager = ContextManager(messages)
    tokens = manager.estimate_tokens()
    assert tokens > 0


def test_is_near_limit():
    config = ContextConfig(max_tokens=100)
    manager = ContextManager([], config=config)
    assert not manager.is_near_limit()

    manager.messages = [Message(role="system", content="x" * 1000)]
    assert manager.is_near_limit()


def test_compact_keeps_recent_messages():
    messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="goal"),
        Message(role="assistant", content="reply 1"),
        Message(role="user", content="question 1"),
        Message(role="assistant", content="reply 2"),
        Message(role="user", content="question 2"),
        Message(role="assistant", content="reply 3"),
    ]
    llm = MockLLM(responses=[AssistantResponse(content="summary text")])
    config = ContextConfig(preserve_recent=2)
    manager = ContextManager(messages, config=config)

    changed = manager.compact(llm)

    assert changed
    assert len(manager.messages) == 4  # system + summary + recent 2
    assert manager.messages[0].role == "system"
    assert manager.messages[0].content == "system prompt"
    assert "summary text" in manager.messages[1].content
    assert manager.messages[2].content == "question 2"
    assert manager.messages[3].content == "reply 3"


def test_compact_not_enough_messages():
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="hi"),
    ]
    llm = MockLLM(responses=[])
    manager = ContextManager(messages)

    changed = manager.compact(llm)

    assert not changed
    assert len(messages) == 2
