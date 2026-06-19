"""Tests for supervisor data models."""

from agent.supervisor.models import (
    AgentRole,
    Goal,
    GoalStatus,
    IPCMessage,
    MessageType,
)


def test_goal_defaults():
    goal = Goal(
        id="g1",
        title="Fix bug",
        description="Fix the login bug",
        agent_role="coder",
    )
    assert goal.status == GoalStatus.PENDING
    assert goal.depends_on == []
    assert goal.error_log == []
    assert goal.artifacts == []
    assert goal.priority == 0
    assert goal.parent_id is None
    assert goal.created_at is not None


def test_goal_status_transitions():
    goal = Goal(id="g1", title="T", agent_role="coder")
    goal.status = GoalStatus.IN_PROGRESS
    assert goal.status == GoalStatus.IN_PROGRESS
    goal.status = GoalStatus.DONE
    assert goal.status == GoalStatus.DONE


def test_agent_role_defaults():
    role = AgentRole(
        name="coder",
        description="Code writer",
        system_prompt="You are a coder.",
    )
    assert role.allowed_tools is None
    assert role.forbidden_tools == []
    assert role.model is None
    assert role.max_steps_per_turn is None
    assert role.temperature is None


def test_agent_role_with_tools():
    role = AgentRole(
        name="architect",
        description="Planner",
        system_prompt="You are an architect.",
        allowed_tools=["read_file", "list_directory"],
        forbidden_tools=["execute_shell"],
        model="kimi-for-coding",
        max_steps_per_turn=50,
        temperature=0.5,
    )
    assert role.allowed_tools == ["read_file", "list_directory"]
    assert role.forbidden_tools == ["execute_shell"]
    assert role.model == "kimi-for-coding"


def test_ipc_message_creation():
    msg = IPCMessage(
        msg_id="m1",
        goal_id="g1",
        type=MessageType.ASSIGN_GOAL,
        payload={"title": "Fix bug"},
    )
    assert msg.timestamp is not None
    assert msg.type == MessageType.ASSIGN_GOAL


def test_ipc_message_serialization():
    msg = IPCMessage(
        msg_id="m1",
        goal_id="g1",
        type=MessageType.STATUS_UPDATE,
        payload={"status": "done"},
    )
    data = msg.model_dump()
    assert data["type"] == "status_update"
    assert data["payload"]["status"] == "done"
