"""Tests for goal persistence."""

import pytest

from agent.supervisor.models import Goal, GoalStatus
from agent.supervisor.persistence import GoalPersistence


@pytest.fixture
def persistence(tmp_path):
    db_path = tmp_path / "goals.db"
    return GoalPersistence(str(db_path))


def test_create_and_get(persistence):
    goal = Goal(id="g1", title="Fix bug", agent_role="coder")
    persistence.create(goal)
    fetched = persistence.get("g1")
    assert fetched is not None
    assert fetched.title == "Fix bug"
    assert fetched.status == GoalStatus.PENDING


def test_get_not_found(persistence):
    assert persistence.get("not_exist") is None


def test_update_status(persistence):
    goal = Goal(id="g1", title="Fix bug", agent_role="coder")
    persistence.create(goal)
    persistence.update_status("g1", GoalStatus.IN_PROGRESS)
    fetched = persistence.get("g1")
    assert fetched.status == GoalStatus.IN_PROGRESS
    assert fetched.started_at is not None


def test_update_status_done(persistence):
    goal = Goal(id="g1", title="Fix bug", agent_role="coder")
    persistence.create(goal)
    persistence.update_status("g1", GoalStatus.DONE, result_summary="fixed")
    fetched = persistence.get("g1")
    assert fetched.status == GoalStatus.DONE
    assert fetched.completed_at is not None
    assert fetched.result_summary == "fixed"


def test_list_active(persistence):
    g1 = Goal(id="g1", title="A", agent_role="coder")
    g2 = Goal(id="g2", title="B", agent_role="coder", status=GoalStatus.DONE)
    g3 = Goal(id="g3", title="C", agent_role="reviewer")
    persistence.create(g1)
    persistence.create(g2)
    persistence.create(g3)

    active = persistence.list_active()
    ids = {g.id for g in active}
    assert ids == {"g1", "g3"}


def test_list_by_role(persistence):
    g1 = Goal(id="g1", title="A", agent_role="coder")
    g2 = Goal(id="g2", title="B", agent_role="reviewer")
    persistence.create(g1)
    persistence.create(g2)

    coder_goals = persistence.list(role="coder")
    assert len(coder_goals) == 1
    assert coder_goals[0].id == "g1"


def test_cancel_and_resume(persistence):
    goal = Goal(id="g1", title="A", agent_role="coder", status=GoalStatus.IN_PROGRESS)
    persistence.create(goal)
    persistence.cancel("g1")
    fetched = persistence.get("g1")
    assert fetched.status == GoalStatus.CANCELLED

    persistence.resume("g1")
    fetched = persistence.get("g1")
    assert fetched.status == GoalStatus.PENDING


def test_list_with_parent(persistence):
    parent = Goal(id="root", title="Root", agent_role="architect")
    child = Goal(id="child", title="Child", agent_role="coder", parent_id="root")
    persistence.create(parent)
    persistence.create(child)

    children = persistence.list(parent_id="root")
    assert len(children) == 1
    assert children[0].id == "child"


def test_add_error_log(persistence):
    goal = Goal(id="g1", title="A", agent_role="coder")
    persistence.create(goal)
    persistence.append_error("g1", "something went wrong")
    fetched = persistence.get("g1")
    assert fetched.error_log == ["something went wrong"]
