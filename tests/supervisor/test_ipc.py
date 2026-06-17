"""Tests for supervisor IPC layer."""

import time
import uuid

import pytest

from agent.supervisor.ipc import IPCClient, IPCServer
from agent.supervisor.models import IPCMessage, MessageType


@pytest.fixture
def ipc_pair():
    # macOS tmp paths are too long for AF_UNIX; use a short path in /tmp.
    socket_path = f"/tmp/ca_test_{uuid.uuid4().hex[:8]}.sock"
    server = IPCServer(socket_path)
    server.start()
    client = IPCClient(socket_path)
    client.connect()
    yield server, client
    client.close()
    server.stop()


def test_send_and_receive_single_message(ipc_pair):
    server, client = ipc_pair

    received = []

    def handler(msg):
        received.append(msg)

    server.set_handler(handler)

    msg = IPCMessage(
        msg_id="m1",
        goal_id="g1",
        type=MessageType.STATUS_UPDATE,
        payload={"status": "in_progress"},
    )
    client.send(msg)

    # Wait for the server to process.
    for _ in range(50):
        if received:
            break
        time.sleep(0.01)

    assert len(received) == 1
    assert received[0].msg_id == "m1"
    assert received[0].type == MessageType.STATUS_UPDATE


def test_roundtrip_response(ipc_pair):
    server, client = ipc_pair

    def handler(msg):
        response = IPCMessage(
            msg_id=str(uuid.uuid4()),
            goal_id=msg.goal_id,
            type=MessageType.TOOL_RESULT,
            payload={"echo": msg.payload},
        )
        server.send_to_client(response)

    server.set_handler(handler)

    request = IPCMessage(
        msg_id="req1",
        goal_id="g1",
        type=MessageType.TOOL_REQUEST,
        payload={"command": "ls"},
    )
    client.send(request)

    response = client.receive(timeout=2.0)
    assert response is not None
    assert response.type == MessageType.TOOL_RESULT
    assert response.payload["echo"]["command"] == "ls"


def test_multiple_messages_in_order(ipc_pair):
    server, client = ipc_pair

    received = []
    server.set_handler(lambda msg: received.append(msg.msg_id))

    for i in range(3):
        client.send(
            IPCMessage(
                msg_id=f"m{i}",
                goal_id="g1",
                type=MessageType.HEARTBEAT,
                payload={},
            )
        )

    for _ in range(50):
        if len(received) == 3:
            break
        time.sleep(0.01)

    assert received == ["m0", "m1", "m2"]


def test_client_reconnect(ipc_pair):
    server, client = ipc_pair

    received = []
    server.set_handler(lambda msg: received.append(msg.msg_id))

    client.send(IPCMessage(msg_id="before", goal_id="g1", type=MessageType.HEARTBEAT))

    client.close()
    client.connect()

    client.send(IPCMessage(msg_id="after", goal_id="g1", type=MessageType.HEARTBEAT))

    for _ in range(50):
        if len(received) == 2:
            break
        time.sleep(0.01)

    assert len(received) == 2


def test_invalid_message_is_ignored(ipc_pair):
    server, client = ipc_pair

    received = []
    server.set_handler(lambda msg: received.append(msg))

    # Send raw invalid JSON.
    client._send_raw(b"not json\n")

    # Send a valid message afterwards.
    client.send(IPCMessage(msg_id="valid", goal_id="g1", type=MessageType.HEARTBEAT))

    for _ in range(50):
        if received:
            break
        time.sleep(0.01)

    assert len(received) == 1
    assert received[0].msg_id == "valid"
