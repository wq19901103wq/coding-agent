"""Tests for worker connection behavior."""

import uuid

import pytest

from agent.config import LLMConfig
from agent.llm.client import LLMClient
from agent.llm.schema import AssistantResponse
from agent.supervisor.ipc import IPCError
from agent.supervisor.role_loader import RoleLoader
from agent.worker.worker import Worker


class FakeLLMClient(LLMClient):
    def __init__(self):
        super().__init__(config=LLMConfig())

    def chat(self, messages, tools=None):
        return AssistantResponse(content="Done")


def test_worker_raises_when_supervisor_unavailable():
    socket_path = f"/tmp/ca_worker_conn_{uuid.uuid4().hex[:8]}.sock"
    worker = Worker(
        socket_address=socket_path,
        workspace="/tmp",
        llm_client=FakeLLMClient(),
        role=RoleLoader().get("coder"),
    )
    with pytest.raises(IPCError):
        worker._connect_with_retry(max_retries=2, delay=0.01)
