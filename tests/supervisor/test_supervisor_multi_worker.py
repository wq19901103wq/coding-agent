"""Tests for supervisor multi-worker support."""

import time
import uuid

from agent.config import Config, LLMConfig
from agent.llm.client import LLMClient
from agent.llm.schema import AssistantResponse
from agent.supervisor.models import GoalStatus
from agent.supervisor.supervisor import Supervisor
from agent.worker.worker import Worker


class FakeLLMClient(LLMClient):
    def __init__(self, responses):
        super().__init__(config=LLMConfig())
        self.responses = responses
        self.call_count = 0

    def chat(self, messages, tools=None):
        response = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return response


def test_supervisor_runs_multiple_goals(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("a")
    (workspace / "b.py").write_text("b")

    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_supervisor_multi_{uuid.uuid4().hex[:8]}.sock"
    config = Config()

    supervisor = Supervisor(
        workspace=str(workspace),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
    )
    supervisor.start()

    responses = [AssistantResponse(content="Done")]

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
        g1 = supervisor.submit_goal(title="Goal 1", description="", agent_role="coder")
        g2 = supervisor.submit_goal(title="Goal 2", description="", agent_role="coder")
        supervisor.run_goal(g1.id)
        supervisor.run_goal(g2.id)

        for _ in range(300):
            f1 = supervisor.persistence.get(g1.id)
            f2 = supervisor.persistence.get(g2.id)
            if f1.status == GoalStatus.DONE and f2.status == GoalStatus.DONE:
                break
            time.sleep(0.01)

        assert supervisor.persistence.get(g1.id).status == GoalStatus.DONE
        assert supervisor.persistence.get(g2.id).status == GoalStatus.DONE
    finally:
        supervisor.stop()
