"""SQLite persistence for goals."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from agent.supervisor.models import Goal, GoalStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    depends_on TEXT, -- JSON list
    title TEXT NOT NULL,
    description TEXT,
    agent_role TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    created_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    result_summary TEXT,
    error_log TEXT, -- JSON list
    artifacts TEXT  -- JSON list
);
"""


def _now() -> str:
    return datetime.utcnow().isoformat()


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class GoalPersistence:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.path.expanduser("~/.coding-agent/goals.db")
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(SCHEMA)

    def create(self, goal: Goal) -> None:
        data = goal.model_dump()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO goals (
                    id, parent_id, depends_on, title, description, agent_role,
                    status, priority, created_at, started_at, completed_at,
                    result_summary, error_log, artifacts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["id"],
                    data["parent_id"],
                    json.dumps(data["depends_on"]),
                    data["title"],
                    data["description"],
                    data["agent_role"],
                    data["status"],
                    data["priority"],
                    data["created_at"],
                    data["started_at"],
                    data["completed_at"],
                    data["result_summary"],
                    json.dumps(data["error_log"]),
                    json.dumps(data["artifacts"]),
                ),
            )

    def get(self, goal_id: str) -> Goal | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_goal(row)

    def update_status(
        self,
        goal_id: str,
        status: GoalStatus,
        result_summary: str | None = None,
    ) -> None:
        now = _now()
        fields = ["status = ?"]
        params: list = [status.value]
        if status == GoalStatus.IN_PROGRESS:
            fields.append("started_at = ?")
            params.append(now)
        elif status in (GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.CANCELLED):
            fields.append("completed_at = ?")
            params.append(now)
        if result_summary is not None:
            fields.append("result_summary = ?")
            params.append(result_summary)
        params.append(goal_id)
        with self._connection() as conn:
            conn.execute(
                f"UPDATE goals SET {', '.join(fields)} WHERE id = ?",
                params,
            )

    def cancel(self, goal_id: str) -> None:
        self.update_status(goal_id, GoalStatus.CANCELLED)

    def resume(self, goal_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE goals SET status = ?, completed_at = NULL WHERE id = ?",
                (GoalStatus.PENDING.value, goal_id),
            )

    def append_error(self, goal_id: str, error: str) -> None:
        goal = self.get(goal_id)
        if goal is None:
            return
        error_log = goal.error_log + [error]
        with self._connection() as conn:
            conn.execute(
                "UPDATE goals SET error_log = ? WHERE id = ?",
                (json.dumps(error_log), goal_id),
            )

    def list_goals(
        self,
        status: GoalStatus | None = None,
        role: str | None = None,
        parent_id: str | None = None,
    ) -> list[Goal]:
        sql = "SELECT * FROM goals WHERE 1=1"
        params: list = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        if role is not None:
            sql += " AND agent_role = ?"
            params.append(role)
        if parent_id is not None:
            sql += " AND parent_id = ?"
            params.append(parent_id)
        sql += " ORDER BY created_at ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def list_active(self) -> list[Goal]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM goals WHERE status IN (?, ?) ORDER BY created_at ASC",
                (GoalStatus.PENDING.value, GoalStatus.IN_PROGRESS.value),
            ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def list_all(self) -> list[Goal]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM goals ORDER BY created_at ASC").fetchall()
        return [self._row_to_goal(row) for row in rows]

    def _row_to_goal(self, row: sqlite3.Row) -> Goal:
        return Goal(
            id=row["id"],
            parent_id=row["parent_id"],
            depends_on=json.loads(row["depends_on"] or "[]"),
            title=row["title"],
            description=row["description"] or "",
            agent_role=row["agent_role"],
            status=GoalStatus(row["status"]),
            priority=row["priority"] or 0,
            created_at=_parse_datetime(row["created_at"]) or datetime.utcnow(),
            started_at=_parse_datetime(row["started_at"]),
            completed_at=_parse_datetime(row["completed_at"]),
            result_summary=row["result_summary"],
            error_log=json.loads(row["error_log"] or "[]"),
            artifacts=json.loads(row["artifacts"] or "[]"),
        )
