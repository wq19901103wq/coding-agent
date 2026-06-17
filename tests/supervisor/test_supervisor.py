"""Tests for supervisor orchestrator."""


import pytest

from agent.config import Config
from agent.supervisor.models import Goal, GoalStatus
from agent.supervisor.persistence import GoalPersistence
from agent.supervisor.scheduler import Scheduler
from agent.supervisor.supervisor import Supervisor


@pytest.fixture
def supervisor(tmp_path):
    db_path = tmp_path / "goals.db"
    socket_path = str(tmp_path / "supervisor.sock")
    config = Config()
    config.history.db_path = str(tmp_path / "history.db")
    return Supervisor(
        workspace=str(tmp_path),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
    )


def test_submit_goal_persists_and_starts(supervisor, tmp_path):
    goal = supervisor.submit_goal(
        title="Read file",
        description="Read hello.py",
        agent_role="coder",
    )
    assert goal.status == GoalStatus.PENDING

    persistence = GoalPersistence(supervisor.db_path)
    fetched = persistence.get(goal.id)
    assert fetched is not None
    assert fetched.title == "Read file"


def test_scheduler_simple_goal():
    goal = Goal(id="g1", title="A", agent_role="coder")
    scheduler = Scheduler([goal])
    ready = scheduler.ready_goals()
    assert len(ready) == 1
    assert ready[0].id == "g1"


def test_scheduler_respects_dependencies():
    g1 = Goal(id="g1", title="A", agent_role="coder")
    g2 = Goal(id="g2", title="B", agent_role="coder", depends_on=["g1"])
    scheduler = Scheduler([g1, g2])

    ready = scheduler.ready_goals()
    assert len(ready) == 1
    assert ready[0].id == "g1"

    scheduler.mark_done("g1")
    ready = scheduler.ready_goals()
    assert len(ready) == 1
    assert ready[0].id == "g2"


def test_supervisor_builds_system_prompt(supervisor):
    prompt = supervisor._build_system_prompt()
    assert "coding-agent" in prompt or "编程助手" in prompt


def test_supervisor_uses_role_system_prompt(supervisor):
    prompt = supervisor._build_system_prompt(role_name="architect")
    assert "架构师" in prompt or "architect" in prompt.lower()
