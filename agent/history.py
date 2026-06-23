import json
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from agent.llm.schema import Message, ToolCall

TodoStatus = Literal["pending", "in_progress", "done"]

DEFAULT_DB_PATH = "~/.coding-agent/history.db"


class HistoryManager:
    """基于 SQLite 的对话历史、会话和待办事项持久化管理器。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or DEFAULT_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    title TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS todos (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                """
            )
            self._migrate_sessions_title(conn)

    def _migrate_sessions_title(self, conn: sqlite3.Connection) -> None:
        """为已存在的 sessions 表添加 title 列（兼容旧数据库）。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "title" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")

    def prune_old_sessions(self, keep: int = 200, vacuum: bool = True) -> int:
        """Delete the oldest sessions beyond ``keep`` and return the count removed.

        Sessions are ordered by ``updated_at`` (then rowid) so the most
        recently touched ones survive. Their messages and todos are removed
        via the ON DELETE CASCADE foreign keys. A ``VACUUM`` reclaims the
        freed space afterwards when ``vacuum`` is True.

        This keeps ``~/.coding-agent/history.db`` from growing unbounded
        (it had reached ~150MB after sustained local use).
        """
        # keep<=0 disables pruning (used by CODING_AGENT_HISTORY_KEEP=0).
        if keep <= 0:
            return 0
        with self._connect() as conn:
            stale = conn.execute(
                "SELECT id FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT -1 OFFSET ?",
                (keep,),
            ).fetchall()
            if not stale:
                return 0
            removed = len(stale)
            conn.executemany(
                "DELETE FROM messages WHERE session_id = ?",
                [(row[0],) for row in stale],
            )
            conn.executemany("DELETE FROM todos WHERE session_id = ?", [(row[0],) for row in stale])
            conn.executemany("DELETE FROM sessions WHERE id = ?", [(row[0],) for row in stale])
        if vacuum and removed:
            # VACUUM cannot run inside a transaction; open a fresh connection.
            with self._connect() as conn:
                conn.execute("VACUUM")
        return removed

    def create_session(self, workspace: str) -> str:
        """创建新会话并返回会话 ID。"""
        session_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, workspace) VALUES (?, ?)",
                (session_id, workspace),
            )
        return session_id

    def get_or_create_session(self, workspace: str) -> str:
        """获取 workspace 最近的会话，不存在则创建新会话。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE workspace = ? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (workspace,),
            ).fetchone()
        if row is not None:
            return str(row[0])
        return self.create_session(workspace)

    def list_recent_sessions(self, limit: int = 5) -> list[dict]:
        """返回最近的会话列表，按更新时间倒序。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, workspace, title, created_at, updated_at FROM sessions "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict | None:
        """返回指定会话信息，不存在则返回 None。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, workspace, title, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def rename_session(self, session_id: str, title: str) -> None:
        """重命名会话标题。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (title, session_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Session '{session_id}' not found")

    def delete_session(self, session_id: str) -> None:
        """删除指定会话及其消息和待办。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def _touch_session(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )

    def clear_session(self, session_id: str) -> None:
        """删除指定会话的所有消息。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._touch_session(conn, session_id)

    def save_message(self, session_id: str, msg: Message) -> None:
        """保存一条消息到指定会话。"""
        tool_calls_json = None
        if msg.tool_calls is not None:
            tool_calls_json = json.dumps([tc.model_dump() for tc in msg.tool_calls])

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    msg.role,
                    msg.content,
                    tool_calls_json,
                    msg.tool_call_id,
                ),
            )
            self._touch_session(conn, session_id)

    def load_messages(self, session_id: str, limit: int = 20) -> list[Message]:
        """按时间顺序返回会话的最近消息。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_call_id FROM messages "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()

        messages: list[Message] = []
        for role, content, tool_calls_json, tool_call_id in reversed(rows):
            tool_calls = None
            if tool_calls_json is not None:
                tool_calls = [ToolCall(**data) for data in json.loads(tool_calls_json)]
            messages.append(
                Message(
                    role=role,
                    content=content,
                    tool_calls=tool_calls,
                    tool_call_id=tool_call_id,
                )
            )
        return messages

    def create_todo(self, session_id: str, title: str, todo_id: str | None = None) -> str:
        """创建待办事项并返回其 ID；若 ID 已存在则幂等返回原 ID。"""
        if todo_id is None:
            todo_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO todos (id, session_id, title) VALUES (?, ?, ?)",
                (todo_id, session_id, title),
            )
        return todo_id

    def update_todo(
        self,
        todo_id: str,
        title: str | None = None,
        status: TodoStatus | None = None,
    ) -> None:
        """更新待办事项的标题和/或状态。"""
        fields: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
        params: list = []
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        params.append(todo_id)

        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE todos SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Todo '{todo_id}' not found")

    def complete_todo(self, todo_id: str) -> None:
        """将待办事项标记为完成。"""
        self.update_todo(todo_id, status="done")

    def list_todos(self, session_id: str) -> list[dict]:
        """返回指定会话下的所有待办事项。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, session_id, title, status, created_at, updated_at "
                "FROM todos WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def load_session(workspace: str, limit: int = 20) -> list[Message]:
    """加载 workspace 最近会话的消息；没有会话则返回空列表。"""
    mgr = HistoryManager()
    with mgr._connect() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE workspace = ? "
            "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (workspace,),
        ).fetchone()
    if row is None:
        return []
    return mgr.load_messages(row[0], limit=limit)
