"""Tests for supervisor tool execution safety."""


import pytest

from agent.config import Config, SecurityConfig
from agent.llm.schema import ToolCall
from agent.supervisor.supervisor import Supervisor


@pytest.fixture
def supervisor(tmp_path):
    config = Config(security=SecurityConfig(confirm_dangerous=False))
    return Supervisor(
        workspace=str(tmp_path),
        config=config,
        db_path=str(tmp_path / "goals.db"),
    )


def test_forbidden_tool_by_role(supervisor, tmp_path):
    goal = supervisor.submit_goal(
        title="Test forbidden tool",
        description="",
        agent_role="architect",
    )
    call = ToolCall(id="c1", name="write_file", arguments={"path": "x.py", "content": "1"})
    result = supervisor._execute_tool(call, goal=goal)
    assert not result.success
    assert "not allowed" in result.error


def test_forbidden_shell_command(supervisor, tmp_path):
    goal = supervisor.submit_goal(
        title="Test forbidden shell",
        description="",
        agent_role="coder",
    )
    call = ToolCall(id="c1", name="execute_shell", arguments={"command": "sudo ls"})
    result = supervisor._execute_tool(call, goal=goal)
    assert not result.success
    assert "forbidden" in result.error.lower()


def test_dangerous_shell_yolo_mode(supervisor, tmp_path):
    goal = supervisor.submit_goal(
        title="Test dangerous shell",
        description="",
        agent_role="coder",
    )
    call = ToolCall(id="c1", name="execute_shell", arguments={"command": "rm file.txt"})
    result = supervisor._execute_tool(call, goal=goal)
    # In YOLO mode the dangerous command is allowed to execute against a missing file.
    assert result.success is False
    assert "file.txt" in (result.output or result.error or "")


def test_dangerous_shell_safe_mode_with_callback(tmp_path):
    config = Config(security=SecurityConfig(confirm_dangerous=True))
    supervisor = Supervisor(
        workspace=str(tmp_path),
        config=config,
        db_path=str(tmp_path / "goals.db"),
        confirm_callback=lambda prompt: True,
    )
    goal = supervisor.submit_goal(
        title="Test dangerous shell",
        description="",
        agent_role="coder",
    )
    call = ToolCall(id="c1", name="execute_shell", arguments={"command": "rm file.txt"})
    result = supervisor._execute_tool(call, goal=goal)
    assert result.success is False
    assert "file.txt" in (result.output or result.error or "")


def test_dangerous_shell_safe_mode_denied(tmp_path):
    config = Config(security=SecurityConfig(confirm_dangerous=True))
    supervisor = Supervisor(
        workspace=str(tmp_path),
        config=config,
        db_path=str(tmp_path / "goals.db"),
        confirm_callback=lambda prompt: False,
    )
    goal = supervisor.submit_goal(
        title="Test dangerous shell",
        description="",
        agent_role="coder",
    )
    call = ToolCall(id="c1", name="execute_shell", arguments={"command": "rm file.txt"})
    result = supervisor._execute_tool(call, goal=goal)
    assert not result.success
    assert "denied" in result.error.lower()
