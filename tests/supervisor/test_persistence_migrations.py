"""Tests for GoalPersistence schema migration and db path resolution."""

import os

from agent.supervisor.models import Goal
from agent.supervisor.persistence import GoalPersistence, resolve_db_path


def test_resolve_db_path_priority(tmp_path, monkeypatch):
    # Highest priority: environment variable.
    env_path = str(tmp_path / "env.db")
    monkeypatch.setenv("CODING_AGENT_GOALS_DB", env_path)
    assert resolve_db_path(str(tmp_path / "ws")) == env_path
    monkeypatch.delenv("CODING_AGENT_GOALS_DB")

    # Second priority: workspace path.
    ws_path = str(tmp_path / "ws" / ".coding-agent" / "goals.db")
    assert resolve_db_path(str(tmp_path / "ws")) == ws_path

    # Fallback: home directory.
    home = os.path.expanduser("~")
    assert resolve_db_path(None) == os.path.join(home, ".coding-agent", "goals.db")


def test_persistence_migrates_old_schema(tmp_path):
    db_path = tmp_path / "goals.db"
    # Create an old-schema database manually.
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE goals (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            depends_on TEXT,
            title TEXT NOT NULL,
            description TEXT,
            agent_role TEXT NOT NULL,
            status TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            created_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            result_summary TEXT,
            error_log TEXT,
            artifacts TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    persistence = GoalPersistence(str(db_path))
    goal = Goal(id="g1", title="Test", agent_role="coder")
    persistence.create(goal)
    fetched = persistence.get("g1")
    assert fetched is not None
    assert fetched.retry_count == 0
    assert fetched.timeout_seconds is None
    assert fetched.cancellation_requested is False
    assert fetched.context == {}


def test_persistence_db_permissions(tmp_path):
    db_path = tmp_path / "goals.db"
    GoalPersistence(str(db_path))
    assert oct(db_path.stat().st_mode)[-3:] == "600"
