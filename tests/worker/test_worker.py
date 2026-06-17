"""Integration tests for worker process."""

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


def test_worker_executes_goal_and_reports_complete():
    socket_path = f"/tmp/ca_worker_test_{uuid.uuid4().hex[:8]}.sock"
    server = IPCServer(socket_path)
    server.start()

    received_messages: list[IPCMessage] = []
    server.set_handler(lambda msg: received_messages.append(msg))

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
        AssistantResponse(content="Done"),
    ]
    worker = Worker(
        socket_address=socket_path,
        workspace="/tmp",
        llm_client=FakeLLMClient(responses),
        role=RoleLoader().get("coder"),
    )

    worker_thread = threading.Thread(target=worker.run, daemon=True)
    worker_thread.start()

    # Wait for worker to connect.
    for _ in range(100):
        if server._client_socket is not None:
            break
        time.sleep(0.01)

    # Send assignment.
    goal = Goal(id="g1", title="Read file", agent_role="coder")
    server.send_to_client(
        IPCMessage(
            msg_id="assign_1",
            goal_id="g1",
            type=MessageType.ASSIGN_GOAL,
            payload={"goal": goal.model_dump()},
        )
    )

    # Wait for tool request.
    for _ in range(100):
        if any(m.type == MessageType.TOOL_REQUEST for m in received_messages):
            break
        time.sleep(0.01)

    tool_request = [m for m in received_messages if m.type == MessageType.TOOL_REQUEST][0]
    assert tool_request.goal_id == "g1"

    # Return tool result.
    server.send_to_client(
        IPCMessage(
            msg_id="result_1",
            goal_id="g1",
            type=MessageType.TOOL_RESULT,
            payload={
                "success": True,
                "output": "print('hello')",
                "error": None,
                "metadata": None,
            },
        )
    )

    # Wait for completion.
    for _ in range(100):
        if any(m.type == MessageType.COMPLETE for m in received_messages):
            break
        time.sleep(0.01)

    complete_msgs = [m for m in received_messages if m.type == MessageType.COMPLETE]
    assert len(complete_msgs) == 1
    assert complete_msgs[0].payload["result"] == "Done"

    worker.ipc.close()
    server.stop()
