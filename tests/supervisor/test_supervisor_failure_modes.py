"""Failure-mode tests for the supervisor orchestrator."""

import time
import uuid

from agent.config import Config, LLMConfig
from agent.llm.client import LLMClient
from agent.llm.schema import AssistantResponse, ToolCall
from agent.supervisor.ipc import IPCClient
from agent.supervisor.models import GoalStatus, IPCMessage, MessageType
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


def test_worker_error_marks_goal_failed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_supervisor_err_{uuid.uuid4().hex[:8]}.sock"
    config = Config()

    supervisor = Supervisor(
        workspace=str(workspace),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
    )
    supervisor.start()

    def spawn_worker(socket_address: str, goal, cfg: Config):
        worker = Worker(
            socket_address=socket_address,
            workspace=str(workspace),
            llm_client=FakeLLMClient([AssistantResponse(content="boom")]),
            role=__import__("agent.supervisor.role_loader", fromlist=["RoleLoader"])
            .RoleLoader()
            .get("coder"),
        )

        def failing_run():
            worker._connect_with_retry()
            worker.ipc.send(
                IPCMessage(
                    msg_id="ready",
                    type=MessageType.READY,
                    payload={"role": worker.role.name},
                )
            )
            assign = worker.ipc.receive(timeout=5.0)
            if assign is None:
                return
            worker.goal = __import__("agent.supervisor.models", fromlist=["Goal"]).Goal(
                **assign.payload["goal"]
            )
            worker.ipc.send(
                IPCMessage(
                    msg_id="e1",
                    goal_id=worker.goal.id,
                    type=MessageType.ERROR,
                    payload={"error": "simulated worker failure"},
                )
            )
            worker.ipc.close()

        worker.run = failing_run  # type: ignore[method-assign]
        worker.run()
        return None

    supervisor._spawn_worker = spawn_worker

    try:
        goal = supervisor.submit_goal(title="Fail", description="", agent_role="coder")
        supervisor.run_goal(goal.id)

        for _ in range(200):
            fetched = supervisor.persistence.get(goal.id)
            if fetched.status == GoalStatus.FAILED:
                break
            time.sleep(0.01)

        fetched = supervisor.persistence.get(goal.id)
        assert fetched.status == GoalStatus.FAILED
        assert any("simulated worker failure" in e for e in fetched.error_log)
    finally:
        supervisor.stop()


def test_watchdog_kills_unresponsive_worker(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_supervisor_wd_{uuid.uuid4().hex[:8]}.sock"
    config = Config()

    supervisor = Supervisor(
        workspace=str(workspace),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
    )
    supervisor.worker_timeout_seconds = 0.3
    supervisor.heartbeat_interval_seconds = 0.1
    supervisor.start()

    def spawn_worker(socket_address: str, goal, cfg: Config):
        worker = Worker(
            socket_address=socket_address,
            workspace=str(workspace),
            llm_client=FakeLLMClient([AssistantResponse(content="hang")]),
            role=__import__("agent.supervisor.role_loader", fromlist=["RoleLoader"])
            .RoleLoader()
            .get("coder"),
        )

        def hang():
            worker._connect_with_retry()
            worker.ipc.send(
                IPCMessage(
                    msg_id="ready",
                    type=MessageType.READY,
                    payload={"role": worker.role.name},
                )
            )
            # Wait for assignment, then do nothing (no heartbeat).
            worker.ipc.receive(timeout=5.0)
            time.sleep(10.0)
            worker.ipc.close()

        worker.run = hang  # type: ignore[method-assign]
        worker.run()
        return None

    supervisor._spawn_worker = spawn_worker

    try:
        goal = supervisor.submit_goal(title="Hang", description="", agent_role="coder")
        supervisor.run_goal(goal.id)

        for _ in range(300):
            fetched = supervisor.persistence.get(goal.id)
            if fetched.status == GoalStatus.FAILED:
                break
            time.sleep(0.01)

        fetched = supervisor.persistence.get(goal.id)
        assert fetched.status == GoalStatus.FAILED
    finally:
        supervisor.stop()


def test_unknown_role_tool_request_is_rejected(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_supervisor_role_{uuid.uuid4().hex[:8]}.sock"
    config = Config()

    supervisor = Supervisor(
        workspace=str(workspace),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
    )
    supervisor.start()

    tool_result: IPCMessage | None = None

    def spawn_worker(socket_address: str, goal, cfg: Config):
        nonlocal tool_result
        client = IPCClient(socket_address)
        client.connect(timeout=5.0)
        client.send(
            IPCMessage(
                msg_id="ready",
                type=MessageType.READY,
                payload={"role": goal.agent_role},
            )
        )
        assign = client.receive(timeout=5.0)
        if assign is None:
            return None
        client.send(
            IPCMessage(
                msg_id="tr",
                goal_id=goal.id,
                type=MessageType.TOOL_REQUEST,
                payload={
                    "tool_call": ToolCall(
                        id="c1", name="read_file", arguments={"path": "x.py"}
                    ).model_dump()
                },
            )
        )
        tool_result = client.receive(timeout=5.0)
        client.close()
        return None

    supervisor._spawn_worker = spawn_worker

    try:
        goal = supervisor.submit_goal(title="Unknown role", description="", agent_role="nosuchrole")
        supervisor.run_goal(goal.id)

        for _ in range(200):
            if tool_result is not None:
                break
            time.sleep(0.01)

        assert tool_result is not None
        assert tool_result.type == MessageType.TOOL_RESULT
        assert not tool_result.payload["success"]
        assert "unknown role" in (tool_result.payload["error"] or "").lower()
    finally:
        supervisor.stop()


def test_dangerous_shell_rejected_without_confirm_callback(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_supervisor_sh_{uuid.uuid4().hex[:8]}.sock"
    config = Config()
    config.security.confirm_dangerous = True

    supervisor = Supervisor(
        workspace=str(workspace),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
        confirm_callback=None,
    )
    supervisor.start()

    tool_result: IPCMessage | None = None

    def spawn_worker(socket_address: str, goal, cfg: Config):
        nonlocal tool_result
        client = IPCClient(socket_address)
        client.connect(timeout=5.0)
        client.send(
            IPCMessage(
                msg_id="ready",
                type=MessageType.READY,
                payload={"role": goal.agent_role},
            )
        )
        assign = client.receive(timeout=5.0)
        if assign is None:
            return None
        client.send(
            IPCMessage(
                msg_id="tr",
                goal_id=goal.id,
                type=MessageType.TOOL_REQUEST,
                payload={
                    "tool_call": ToolCall(
                        id="c1",
                        name="execute_shell",
                        arguments={"command": "rm file.txt"},
                    ).model_dump()
                },
            )
        )
        tool_result = client.receive(timeout=5.0)
        client.close()
        return None

    supervisor._spawn_worker = spawn_worker

    try:
        goal = supervisor.submit_goal(title="Dangerous shell", description="", agent_role="coder")
        supervisor.run_goal(goal.id)

        for _ in range(200):
            if tool_result is not None:
                break
            time.sleep(0.01)

        assert tool_result is not None
        assert tool_result.type == MessageType.TOOL_RESULT
        assert not tool_result.payload["success"]
        assert "requires user confirmation" in (tool_result.payload["error"] or "").lower()
    finally:
        supervisor.stop()
