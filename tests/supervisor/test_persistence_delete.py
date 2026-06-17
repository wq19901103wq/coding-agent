"""Tests for GoalPersistence.delete."""

from agent.supervisor.models import Goal, GoalStatus
from agent.supervisor.persistence import GoalPersistence


def test_delete_goal(tmp_path):
    db_path = tmp_path / "goals.db"
    persistence = GoalPersistence(str(db_path))
    goal = Goal(id="g1", title="A", agent_role="coder")
    persistence.create(goal)

    assert persistence.delete("g1") is True
    assert persistence.get("g1") is None


def test_delete_only_done_goals(tmp_path):
    db_path = tmp_path / "goals.db"
    persistence = GoalPersistence(str(db_path))
    done = Goal(id="done", title="Done", agent_role="coder", status=GoalStatus.DONE)
    pending = Goal(id="pending", title="Pending", agent_role="coder")
    persistence.create(done)
    persistence.create(pending)

    persistence.delete("done")

    assert persistence.get("done") is None
    assert persistence.get("pending") is not None
