import json
import uuid
from pathlib import Path

import pytest

from agent.history import HistoryManager, load_session
from agent.llm.schema import Message, ToolCall


@pytest.fixture
def history(tmp_path):
    """提供一个使用临时数据库的 HistoryManager 实例。"""
    db_path = tmp_path / "history.db"
    return HistoryManager(str(db_path))


@pytest.fixture
def sample_workspace():
    return "/tmp/sample-project"


class TestDatabaseInit:
    def test_default_db_path_uses_home(self, isolated_home):
        mgr = HistoryManager()
        expected = Path(isolated_home) / ".coding-agent" / "history.db"
        assert mgr.db_path == expected
        assert expected.exists()

    def test_custom_db_path(self, tmp_path):
        db_path = tmp_path / "custom" / "history.db"
        mgr = HistoryManager(str(db_path))
        assert mgr.db_path == db_path
        assert db_path.exists()

    def test_tables_are_created(self, history):
        with history._connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"sessions", "messages", "todos"}.issubset(tables)


class TestSessions:
    def test_create_session_returns_uuid(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        assert isinstance(session_id, str)
        assert uuid.UUID(session_id)

    def test_get_or_create_session_creates_new(self, history, sample_workspace):
        session_id = history.get_or_create_session(sample_workspace)
        assert isinstance(session_id, str)
        assert uuid.UUID(session_id)

    def test_get_or_create_session_returns_existing(self, history, sample_workspace):
        first = history.get_or_create_session(sample_workspace)
        second = history.get_or_create_session(sample_workspace)
        assert first == second

    def test_list_recent_sessions(self, history):
        sid1 = history.create_session("/tmp/a")
        sid2 = history.create_session("/tmp/b")
        sid3 = history.create_session("/tmp/c")

        sessions = history.list_recent_sessions(limit=5)
        assert len(sessions) == 3
        assert sessions[0]["id"] == sid3
        assert sessions[1]["id"] == sid2
        assert sessions[2]["id"] == sid1

        for s in sessions:
            assert "id" in s
            assert "workspace" in s
            assert "created_at" in s
            assert "updated_at" in s

    def test_list_recent_sessions_respects_limit(self, history):
        for i in range(7):
            history.create_session(f"/tmp/proj{i}")
        sessions = history.list_recent_sessions(limit=5)
        assert len(sessions) == 5


class TestMessages:
    def test_save_and_load_text_message(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        msg = Message(role="user", content="hello")
        history.save_message(session_id, msg)

        loaded = history.load_messages(session_id)
        assert len(loaded) == 1
        assert loaded[0].role == "user"
        assert loaded[0].content == "hello"

    def test_load_messages_returns_chronological_order(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        history.save_message(session_id, Message(role="user", content="first"))
        history.save_message(session_id, Message(role="assistant", content="second"))
        history.save_message(session_id, Message(role="user", content="third"))

        loaded = history.load_messages(session_id)
        assert [m.content for m in loaded] == ["first", "second", "third"]

    def test_load_messages_limit(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        for i in range(10):
            history.save_message(session_id, Message(role="user", content=f"msg{i}"))

        loaded = history.load_messages(session_id, limit=3)
        assert len(loaded) == 3
        assert [m.content for m in loaded] == ["msg7", "msg8", "msg9"]

    def test_save_and_load_tool_calls(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        tool_calls = [
            ToolCall(id="call-1", name="write_file", arguments={"path": "a.py"})
        ]
        msg = Message(role="assistant", content=None, tool_calls=tool_calls)
        history.save_message(session_id, msg)

        loaded = history.load_messages(session_id)
        assert len(loaded) == 1
        assert loaded[0].tool_calls is not None
        assert len(loaded[0].tool_calls) == 1
        assert loaded[0].tool_calls[0].id == "call-1"
        assert loaded[0].tool_calls[0].name == "write_file"
        assert loaded[0].tool_calls[0].arguments == {"path": "a.py"}

    def test_save_and_load_tool_response(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        msg = Message(role="tool", content="ok", tool_call_id="call-1")
        history.save_message(session_id, msg)

        loaded = history.load_messages(session_id)
        assert len(loaded) == 1
        assert loaded[0].role == "tool"
        assert loaded[0].tool_call_id == "call-1"

    def test_save_message_updates_session_timestamp(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        before = history.list_recent_sessions(limit=1)[0]["updated_at"]
        history.save_message(session_id, Message(role="user", content="hi"))
        after = history.list_recent_sessions(limit=1)[0]["updated_at"]
        assert after >= before


class TestTodos:
    def test_create_todo(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        todo_id = history.create_todo(session_id, "实现功能")
        assert isinstance(todo_id, str)

        todos = history.list_todos(session_id)
        assert len(todos) == 1
        assert todos[0]["id"] == todo_id
        assert todos[0]["title"] == "实现功能"
        assert todos[0]["status"] == "pending"

    def test_create_todo_with_custom_id(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        todo_id = history.create_todo(session_id, "测试", todo_id="todo-1")
        assert todo_id == "todo-1"

    def test_update_todo_title_and_status(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        todo_id = history.create_todo(session_id, "原标题")
        history.update_todo(todo_id, title="新标题", status="in_progress")

        todos = history.list_todos(session_id)
        assert todos[0]["title"] == "新标题"
        assert todos[0]["status"] == "in_progress"

    def test_complete_todo(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        todo_id = history.create_todo(session_id, "待完成")
        history.complete_todo(todo_id)

        todos = history.list_todos(session_id)
        assert todos[0]["status"] == "done"

    def test_list_todos_only_for_session(self, history):
        sid1 = history.create_session("/tmp/a")
        sid2 = history.create_session("/tmp/b")
        history.create_todo(sid1, "a任务")
        history.create_todo(sid2, "b任务")

        assert len(history.list_todos(sid1)) == 1
        assert history.list_todos(sid1)[0]["title"] == "a任务"

    def test_update_todo_not_found(self, history):
        with pytest.raises(ValueError, match="Todo 'missing' not found"):
            history.update_todo("missing", title="x")

    def test_complete_todo_not_found(self, history):
        with pytest.raises(ValueError, match="Todo 'missing' not found"):
            history.complete_todo("missing")


class TestReinitAndRecovery:
    def test_reinitialize_existing_db_preserves_data(self, tmp_path):
        db_path = tmp_path / "history.db"
        mgr = HistoryManager(str(db_path))
        session_id = mgr.create_session("/tmp/preserved")
        mgr.save_message(session_id, Message(role="user", content="keep me"))

        new_mgr = HistoryManager(str(db_path))
        loaded = new_mgr.load_messages(session_id)
        assert len(loaded) == 1
        assert loaded[0].content == "keep me"

    def test_load_messages_recover_recent_20_from_1000(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        for i in range(1000):
            history.save_message(session_id, Message(role="user", content=f"msg{i}"))

        loaded = history.load_messages(session_id)
        assert len(loaded) == 20
        assert [m.content for m in loaded] == [f"msg{i}" for i in range(980, 1000)]

    def test_save_and_load_10000_char_content(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        long_content = "x" * 10000
        history.save_message(session_id, Message(role="user", content=long_content))

        loaded = history.load_messages(session_id)
        assert len(loaded) == 1
        assert len(loaded[0].content) == 10000
        assert loaded[0].content == long_content

    def test_empty_session_returns_empty_list(self, history, sample_workspace):
        session_id = history.create_session(sample_workspace)
        loaded = history.load_messages(session_id)
        assert loaded == []


class TestLoadSession:
    def test_load_session_returns_messages_for_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mgr = HistoryManager()
        session_id = mgr.create_session("/tmp/ws")
        mgr.save_message(session_id, Message(role="user", content="hello via load_session"))

        loaded = load_session("/tmp/ws")
        assert len(loaded) == 1
        assert loaded[0].content == "hello via load_session"

    def test_load_session_returns_empty_list_when_no_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        loaded = load_session("/tmp/nonexistent")
        assert loaded == []

    def test_load_session_respects_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mgr = HistoryManager()
        session_id = mgr.create_session("/tmp/ws-limit")
        for i in range(30):
            mgr.save_message(session_id, Message(role="user", content=f"msg{i}"))

        loaded = load_session("/tmp/ws-limit", limit=5)
        assert len(loaded) == 5
        assert [m.content for m in loaded] == [f"msg{i}" for i in range(25, 30)]
