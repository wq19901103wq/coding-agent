import json
from types import SimpleNamespace

from agent.direct_agent import DirectAgent
from agent.llm.schema import AssistantResponse, Usage


class FakeLLM:
    config = SimpleNamespace(max_total_tokens_per_turn=100)

    def chat(self, _messages, tools):
        assert tools == []
        return AssistantResponse(content="done", usage=Usage(total_tokens=7))


def test_direct_agent_records_durable_run_summary(tmp_path):
    trace = tmp_path / "agent.log"
    agent = DirectAgent(FakeLLM(), tmp_path, "test", allowed_tools=[], log_path=trace)

    assert agent.run("fix it") == "done"

    events = [json.loads(line) for line in trace.read_text().splitlines()]
    response = next(event for event in events if event["type"] == "llm_response")
    finished = events[-1]
    assert response["usage"]["total_tokens"] == 7
    assert response["cumulative_tokens"] == 7
    assert finished["type"] == "run_end"
    assert finished["status"] == "completed"
    assert finished["step"] == 1
    assert finished["total_tokens"] == 7
    assert finished["duration_seconds"] >= 0


def test_direct_agent_excludes_interactive_auto_memory_tool(tmp_path):
    agent = DirectAgent(FakeLLM(), tmp_path, "test")

    assert "remember_project_memory" not in agent.tool_names
