"""Tests for worker execution limits."""

import threading
import time
import uuid

from agent.config import LLMConfig
from agent.llm.client import LLMClient
from agent.llm.schema import AssistantResponse, ToolCall
from agent.supervisor.ipc import IPCServer
from agent.supervisor.models import Goal, IPCMessage, MessageType
from agent.supervisor.role_loader import RoleLoader
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


def test_worker_stops_at_max_steps():
    socket_path = f"/tmp/ca_worker_maxsteps_{uuid.uuid4().hex[:8]}.sock"
    server = IPCServer(socket_path)
    server.start()

    received_messages: list[IPCMessage] = []

    def handler(msg, client_id):
        received_messages.append(msg)
        if msg.type == MessageType.TOOL_REQUEST:
            server.send_to_client(
                IPCMessage(
                    msg_id="result",
                    goal_id=msg.goal_id,
                    type=MessageType.TOOL_RESULT,
                    payload={"success": True, "output": "ok", "error": None, "metadata": None},
                ),
                client_id=client_id,
            )

    server.set_handler(handler)

    # LLM always requests a tool call, never produces a final answer.
    responses = [
        AssistantResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"call_{i}",
                    name="read_file",
                    arguments={"path": "hello.py"},
                )
            ],
        )
        for i in range(10)
    ]
    role = RoleLoader().get("coder")
    role.max_steps_per_turn = 3

    worker = Worker(
        socket_address=socket_path,
        workspace="/tmp",
        llm_client=FakeLLMClient(responses),
        role=role,
    )

    worker_thread = threading.Thread(target=worker.run, daemon=True)
    worker_thread.start()

    client_id = None
    for _ in range(100):
        if server._clients:
            client_id = next(iter(server._clients))
            break
        time.sleep(0.01)
    assert client_id is not None

    goal = Goal(id="g1", title="Read file", agent_role="coder")
    server.send_to_client(
        IPCMessage(
            msg_id="assign_1",
            goal_id="g1",
            type=MessageType.ASSIGN_GOAL,
            payload={"goal": goal.model_dump()},
        ),
        client_id=client_id,
    )

    # Wait for worker to report completion or error.
    for _ in range(200):
        if any(m.type in (MessageType.COMPLETE, MessageType.ERROR) for m in received_messages):
            break
        time.sleep(0.01)

    complete_msgs = [m for m in received_messages if m.type == MessageType.COMPLETE]
    assert len(complete_msgs) == 1
    assert "maximum steps" in complete_msgs[0].payload["result"].lower()

    worker.ipc.close()
    server.stop()
