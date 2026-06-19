"""Integration tests for supervisor with a mock worker."""

import time
import uuid

from agent.config import Config, LLMConfig
from agent.llm.client import LLMClient
from agent.llm.schema import AssistantResponse, ToolCall
from agent.supervisor.models import GoalStatus
from agent.supervisor.supervisor import Supervisor
from agent.worker.worker import Worker


class FakeLLMClient(LLMClient):
    def __init__(self, responses):
        super().__init__(config=LLMConfig())
        self.responses = responses
        self.call_count = 0

    def chat(self, messages, tools=None):
        response = self.responses[self.call_count]
        self.call_count += 1
        return response


def test_supervisor_runs_goal_with_mock_worker(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.py").write_text("print('hello')")

    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_supervisor_test_{uuid.uuid4().hex[:8]}.sock"
    config = Config()

    supervisor = Supervisor(
        workspace=str(workspace),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
    )
    supervisor.start()

    responses = [
        AssistantResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="read_file",
                    arguments={"path": "hello.py"},
                )
            ],
        ),
        AssistantResponse(content="File contains hello"),
    ]

    def spawn_worker(socket_address: str, goal, cfg: Config):
        worker = Worker(
            socket_address=socket_address,
            workspace=str(workspace),
            llm_client=FakeLLMClient(responses),
            role=__import__("agent.supervisor.role_loader", fromlist=["RoleLoader"])
            .RoleLoader()
            .get("coder"),
        )
        worker.run()
        return None

    supervisor._spawn_worker = spawn_worker

    try:
        goal = supervisor.submit_goal(
            title="Read hello.py",
            description="Read the file",
            agent_role="coder",
        )
        supervisor.run_goal(goal.id)

        for _ in range(200):
            fetched = supervisor.persistence.get(goal.id)
            if fetched.status == GoalStatus.DONE:
                break
            time.sleep(0.01)

        fetched = supervisor.persistence.get(goal.id)
        assert fetched.status == GoalStatus.DONE
        assert "File contains hello" in (fetched.result_summary or "")
    finally:
        supervisor.stop()
