"""Remaining supervisor tests for edge cases and recovery."""

import threading
import time
import uuid
from unittest.mock import patch

from agent.config import Config
from agent.supervisor.ipc import IPCClient, IPCServer
from agent.supervisor.models import GoalStatus, IPCMessage, MessageType
from agent.supervisor.supervisor import Supervisor


def test_supervisor_recovers_active_goals_after_restart(tmp_path):
    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_supervisor_recover_{uuid.uuid4().hex[:8]}.sock"
    config = Config()

    supervisor = Supervisor(
        workspace=str(tmp_path),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
    )
    goal = supervisor.submit_goal(title="Recover me", description="", agent_role="coder")
    supervisor.start()
    supervisor.stop()

    new_supervisor = Supervisor(
        workspace=str(tmp_path),
        config=config,
        socket_address=socket_path + ".2",
        db_path=str(db_path),
    )
    active = new_supervisor.persistence.list_active()
    assert any(g.id == goal.id for g in active)


def test_unknown_ipc_message_type_is_ignored():
    socket_path = f"/tmp/ca_ipc_unknown_{uuid.uuid4().hex[:8]}.sock"
    server = IPCServer(socket_path)
    server.start()

    received = []
    server.set_handler(lambda msg, _client_id: received.append(msg))

    client = IPCClient(socket_path)
    client.connect()

    # Send a message with a type the server does not handle explicitly.
    client.send(
        IPCMessage(
            msg_id="u1",
            goal_id="g1",
            type=MessageType.USER_INPUT,
            payload={"text": "hello"},
        )
    )

    for _ in range(50):
        if received:
            break
        time.sleep(0.01)

    assert len(received) == 1
    assert received[0].type == MessageType.USER_INPUT

    client.close()
    server.stop()


def test_goal_completed_callback_is_invoked(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_supervisor_cb_{uuid.uuid4().hex[:8]}.sock"
    config = Config()

    completed_goals = []
    completed_event = threading.Event()

    def on_completed(goal):
        completed_goals.append(goal)
        completed_event.set()

    supervisor = Supervisor(
        workspace=str(workspace),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
        goal_completed_callback=on_completed,
    )
    supervisor.start()

    from agent.config import LLMConfig
    from agent.llm.client import LLMClient
    from agent.llm.schema import AssistantResponse
    from agent.worker.worker import Worker

    class FakeLLMClient(LLMClient):
        def __init__(self):
            super().__init__(config=LLMConfig())

        def chat(self, messages, tools=None):
            return AssistantResponse(content="Done")

    def spawn_worker(socket_address: str, goal, cfg: Config):
        worker = Worker(
            socket_address=socket_address,
            workspace=str(workspace),
            llm_client=FakeLLMClient(),
            role=__import__("agent.supervisor.role_loader", fromlist=["RoleLoader"])
            .RoleLoader()
            .get("coder"),
        )
        worker.run()
        return None

    supervisor._spawn_worker = spawn_worker

    try:
        goal = supervisor.submit_goal(title="Callback", description="", agent_role="coder")
        supervisor.run_goal(goal.id)

        for _ in range(200):
            fetched = supervisor.persistence.get(goal.id)
            if fetched.status == GoalStatus.DONE:
                break
            time.sleep(0.01)

        # Wait for the asynchronous callback to actually be invoked.
        completed_event.wait(timeout=5.0)

        assert len(completed_goals) == 1
        assert completed_goals[0].id == goal.id
    finally:
        supervisor.stop()


def test_ipc_tcp_fallback():
    address = "127.0.0.1:17474"
    with patch("agent.supervisor.ipc._can_use_unix_socket", return_value=False):
        server = IPCServer(address)
        server.start()
        client = IPCClient(address)
        client.connect(timeout=2.0)

        received = []
        server.set_handler(lambda msg, _client_id: received.append(msg))

        client.send(IPCMessage(msg_id="t1", goal_id="g1", type=MessageType.HEARTBEAT, payload={}))

        for _ in range(50):
            if received:
                break
            time.sleep(0.01)

        assert len(received) == 1
        assert received[0].msg_id == "t1"

        client.close()
        server.stop()
