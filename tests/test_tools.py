import pytest
from pydantic import BaseModel as PydanticModel
from pydantic import ValidationError

from agent.history import HistoryManager
from agent.tools.base import BaseTool, ToolContext, ToolResult


class DummyInput(PydanticModel):
    x: int


class DummyTool(BaseTool):
    name = "dummy"
    description = "dummy tool"
    input_schema = DummyInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(success=True, output=str(input["x"] * 2))


@pytest.fixture
def isolated_registry(monkeypatch):
    """提供一个被清空并恢复的独立工具注册表副本。"""
    from agent.tools import TOOL_REGISTRY

    original = TOOL_REGISTRY.copy()
    TOOL_REGISTRY.clear()
    yield TOOL_REGISTRY
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(original)


def test_tool_result_success():
    r = ToolResult(success=True, output="hello")
    assert r.success and r.output == "hello"


def test_tool_registry(isolated_registry):
    from agent.tools import register_tool

    register_tool(DummyTool())
    assert "dummy" in isolated_registry


def test_get_tool_found(isolated_registry):
    from agent.tools import get_tool, register_tool

    tool = DummyTool()
    register_tool(tool)
    assert get_tool("dummy") is tool


def test_get_tool_not_found(isolated_registry):
    from agent.tools import get_tool

    with pytest.raises(KeyError, match="Tool 'missing' not found"):
        get_tool("missing")


def test_base_tool_is_abstract():
    with pytest.raises(TypeError):
        BaseTool()


@pytest.fixture
def file_tools(isolated_registry):
    """提供文件工具实例并注册到隔离注册表。"""
    from agent.tools import register_tool
    from agent.tools.read_file import ReadFileTool
    from agent.tools.str_replace_file import StrReplaceFileTool
    from agent.tools.write_file import WriteFileTool

    read_tool = ReadFileTool()
    write_tool = WriteFileTool()
    replace_tool = StrReplaceFileTool()
    register_tool(read_tool)
    register_tool(write_tool)
    register_tool(replace_tool)
    return read_tool, write_tool, replace_tool


@pytest.fixture
def workspace(tmp_path):
    """提供一个隔离的工作目录。"""
    return tmp_path


class TestReadFile:
    def test_read_file_success(self, file_tools, workspace):
        read_tool, _, _ = file_tools
        (workspace / "hello.py").write_text("print('hello')", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = read_tool.execute({"path": "hello.py"}, ctx)

        assert result.success
        assert result.output == "print('hello')"

    def test_read_file_not_found(self, file_tools, workspace):
        read_tool, _, _ = file_tools
        ctx = ToolContext(workspace=str(workspace))

        result = read_tool.execute({"path": "not_exist.py"}, ctx)

        assert not result.success
        assert "File not found" in result.error

    def test_read_file_outside_workspace(self, file_tools, workspace):
        read_tool, _, _ = file_tools
        ctx = ToolContext(workspace=str(workspace))

        result = read_tool.execute({"path": "../outside.txt"}, ctx)

        assert not result.success
        assert "Path outside workspace" in result.error

    def test_read_file_is_directory(self, file_tools, workspace):
        read_tool, _, _ = file_tools
        (workspace / "src").mkdir()
        ctx = ToolContext(workspace=str(workspace))

        result = read_tool.execute({"path": "src"}, ctx)

        assert not result.success
        assert "Is a directory" in result.error

    def test_read_file_truncation(self, file_tools, workspace):
        read_tool, _, _ = file_tools
        (workspace / "big.txt").write_text("a" * 6000, encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = read_tool.execute({"path": "big.txt"}, ctx)

        assert result.success
        assert len(result.output) == 5000
        assert result.metadata.get("truncated") is True
        assert result.metadata.get("original_length") == 6000


class TestWriteFile:
    def test_write_file_create(self, file_tools, workspace):
        _, write_tool, _ = file_tools
        ctx = ToolContext(workspace=str(workspace))

        result = write_tool.execute({"path": "a.py", "content": "x=1"}, ctx)

        assert result.success
        assert (workspace / "a.py").read_text(encoding="utf-8") == "x=1"

    def test_write_file_overwrite(self, file_tools, workspace):
        _, write_tool, _ = file_tools
        (workspace / "a.py").write_text("x=1", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = write_tool.execute({"path": "a.py", "content": "x=2"}, ctx)

        assert result.success
        assert (workspace / "a.py").read_text(encoding="utf-8") == "x=2"

    def test_write_file_append(self, file_tools, workspace):
        _, write_tool, _ = file_tools
        (workspace / "a.py").write_text("x=1", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = write_tool.execute({"path": "a.py", "content": "\ny=2", "append": True}, ctx)

        assert result.success
        assert (workspace / "a.py").read_text(encoding="utf-8") == "x=1\ny=2"

    def test_write_file_outside_workspace(self, file_tools, workspace):
        _, write_tool, _ = file_tools
        ctx = ToolContext(workspace=str(workspace))
        outside = workspace.parent / "x.py"

        result = write_tool.execute({"path": "../x.py", "content": "x=1"}, ctx)

        assert not result.success
        assert "Path outside workspace" in result.error
        assert not outside.exists()

    def test_write_file_is_directory(self, file_tools, workspace):
        _, write_tool, _ = file_tools
        (workspace / "src").mkdir()
        ctx = ToolContext(workspace=str(workspace))

        result = write_tool.execute({"path": "src/", "content": "x=1"}, ctx)

        assert not result.success
        assert (workspace / "src").is_dir()


class TestStrReplaceFile:
    def test_str_replace_success(self, file_tools, workspace):
        _, _, replace_tool = file_tools
        (workspace / "a.py").write_text("x=1\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = replace_tool.execute({"path": "a.py", "old_str": "x=1", "new_str": "x=2"}, ctx)

        assert result.success
        assert (workspace / "a.py").read_text(encoding="utf-8") == "x=2\n"

    def test_str_replace_no_match(self, file_tools, workspace):
        _, _, replace_tool = file_tools
        (workspace / "a.py").write_text("x=1\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = replace_tool.execute(
            {"path": "a.py", "old_str": "not_exist", "new_str": "x=2"}, ctx
        )

        assert not result.success
        assert "No match" in result.error
        assert (workspace / "a.py").read_text(encoding="utf-8") == "x=1\n"

    def test_str_replace_multiple_matches(self, file_tools, workspace):
        _, _, replace_tool = file_tools
        (workspace / "a.py").write_text("aaa", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = replace_tool.execute({"path": "a.py", "old_str": "a", "new_str": "b"}, ctx)

        assert not result.success
        assert "unique" in result.error.lower()
        assert (workspace / "a.py").read_text(encoding="utf-8") == "aaa"

    def test_str_replace_outside_workspace(self, file_tools, workspace):
        _, _, replace_tool = file_tools
        ctx = ToolContext(workspace=str(workspace))

        result = replace_tool.execute({"path": "../x.py", "old_str": "x=1", "new_str": "x=2"}, ctx)

        assert not result.success
        assert "Path outside workspace" in result.error


@pytest.fixture
def dir_search_tools(isolated_registry):
    """提供目录与搜索工具实例并注册到隔离注册表。"""
    from agent.tools import register_tool
    from agent.tools.code_search import CodeSearchTool
    from agent.tools.glob_search import GlobSearchTool
    from agent.tools.list_directory import ListDirectoryTool

    list_tool = ListDirectoryTool()
    glob_tool = GlobSearchTool()
    code_tool = CodeSearchTool()
    register_tool(list_tool)
    register_tool(glob_tool)
    register_tool(code_tool)
    return list_tool, glob_tool, code_tool


class TestListDirectory:
    def test_list_directory_success(self, dir_search_tools, workspace):
        list_tool, _, _ = dir_search_tools
        (workspace / "src").mkdir()
        (workspace / "a.py").write_text("x=1", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = list_tool.execute({"path": "."}, ctx)

        assert result.success
        assert "dir: src" in result.output
        assert "file: a.py" in result.output

    def test_list_directory_default_path(self, dir_search_tools, workspace):
        list_tool, _, _ = dir_search_tools
        (workspace / "b.py").write_text("y=2", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = list_tool.execute({}, ctx)

        assert result.success
        assert "file: b.py" in result.output

    def test_list_directory_not_found(self, dir_search_tools, workspace):
        list_tool, _, _ = dir_search_tools
        ctx = ToolContext(workspace=str(workspace))

        result = list_tool.execute({"path": "missing"}, ctx)

        assert not result.success
        assert "Directory not found" in result.error

    def test_list_directory_not_a_directory(self, dir_search_tools, workspace):
        list_tool, _, _ = dir_search_tools
        (workspace / "a.py").write_text("x", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = list_tool.execute({"path": "a.py"}, ctx)

        assert not result.success
        assert "Not a directory" in result.error

    def test_list_directory_outside_workspace(self, dir_search_tools, workspace):
        list_tool, _, _ = dir_search_tools
        ctx = ToolContext(workspace=str(workspace))

        result = list_tool.execute({"path": "../outside"}, ctx)

        assert not result.success
        assert "Path outside workspace" in result.error


class TestGlobSearch:
    def test_glob_search_success(self, dir_search_tools, workspace):
        _, glob_tool, _ = dir_search_tools
        (workspace / "src").mkdir()
        (workspace / "src" / "a.py").write_text("x=1", encoding="utf-8")
        (workspace / "b.py").write_text("y=2", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = glob_tool.execute({"pattern": "**/*.py"}, ctx)

        assert result.success
        assert "src/a.py" in result.output
        assert "b.py" in result.output

    def test_glob_search_no_matches(self, dir_search_tools, workspace):
        _, glob_tool, _ = dir_search_tools
        ctx = ToolContext(workspace=str(workspace))

        result = glob_tool.execute({"pattern": "*.md"}, ctx)

        assert result.success
        assert "(no matches)" in result.output

    def test_glob_search_outside_workspace(self, dir_search_tools, workspace):
        _, glob_tool, _ = dir_search_tools
        ctx = ToolContext(workspace=str(workspace))

        result = glob_tool.execute({"pattern": "../*.txt"}, ctx)

        assert not result.success
        assert "Path outside workspace" in result.error


class TestCodeSearch:
    def test_code_search_success(self, dir_search_tools, workspace):
        _, _, code_tool = dir_search_tools
        (workspace / "a.py").write_text("x=1\ny=2\n", encoding="utf-8")
        (workspace / "src").mkdir()
        (workspace / "src" / "b.py").write_text("def foo():\n    y=2\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = code_tool.execute({"pattern": "y=2"}, ctx)

        assert result.success
        assert "a.py:2: y=2" in result.output
        assert "src/b.py:2:     y=2" in result.output

    def test_code_search_default_path(self, dir_search_tools, workspace):
        _, _, code_tool = dir_search_tools
        (workspace / "c.py").write_text("foo=1\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = code_tool.execute({"pattern": "foo"}, ctx)

        assert result.success
        assert "c.py:1: foo=1" in result.output

    def test_code_search_no_matches(self, dir_search_tools, workspace):
        _, _, code_tool = dir_search_tools
        (workspace / "d.py").write_text("x=1\n", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = code_tool.execute({"pattern": "notfound"}, ctx)

        assert result.success
        assert "(no matches)" in result.output

    def test_code_search_outside_workspace(self, dir_search_tools, workspace):
        _, _, code_tool = dir_search_tools
        ctx = ToolContext(workspace=str(workspace))

        result = code_tool.execute({"pattern": "x", "path": ".."}, ctx)

        assert not result.success
        assert "Path outside workspace" in result.error

    def test_code_search_invalid_pattern(self, dir_search_tools, workspace):
        _, _, code_tool = dir_search_tools
        ctx = ToolContext(workspace=str(workspace))

        result = code_tool.execute({"pattern": "[invalid"}, ctx)

        assert not result.success
        assert "Invalid regex pattern" in result.error


@pytest.fixture
def shell_tool(isolated_registry):
    """提供 execute_shell 工具实例并注册到隔离注册表。"""
    from agent.tools import register_tool
    from agent.tools.execute_shell import ExecuteShellTool

    tool = ExecuteShellTool()
    register_tool(tool)
    return tool


class TestExecuteShell:
    def test_execute_shell_harmless_success(self, shell_tool, workspace):
        (workspace / "hello.py").write_text("print('hello')", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = shell_tool.execute({"command": "cat hello.py"}, ctx)

        assert result.success
        assert result.output == "print('hello')"

    def test_execute_shell_harmless_in_subdirectory(self, shell_tool, workspace):
        (workspace / "sub").mkdir()
        (workspace / "sub" / "file.txt").write_text("nested", encoding="utf-8")
        ctx = ToolContext(workspace=str(workspace))

        result = shell_tool.execute({"command": "cat sub/file.txt"}, ctx)

        assert result.success
        assert result.output == "nested"

    def test_execute_shell_dangerous_blocked(self, shell_tool, workspace):
        ctx = ToolContext(workspace=str(workspace))
        target = workspace / "should_not_exist.txt"

        result = shell_tool.execute({"command": f"echo x > {target.name}"}, ctx)

        assert not result.success
        assert "dangerous" in result.error.lower()
        assert not target.exists()

    def test_execute_shell_forbidden_blocked(self, shell_tool, workspace):
        ctx = ToolContext(workspace=str(workspace))

        result = shell_tool.execute({"command": "sudo ls -la"}, ctx)

        assert not result.success
        assert "forbidden" in result.error.lower()

    def test_execute_shell_timeout(self, shell_tool, workspace):
        ctx = ToolContext(workspace=str(workspace))

        result = shell_tool.execute(
            {"command": 'python3 -c "import time; time.sleep(5)"', "timeout": 1},
            ctx,
        )

        assert not result.success
        assert "timeout" in result.error.lower() or "timed out" in result.error.lower()

    def test_execute_shell_nonzero_exit(self, shell_tool, workspace):
        ctx = ToolContext(workspace=str(workspace))

        result = shell_tool.execute({"command": "cat nonexistent_file.txt"}, ctx)

        assert not result.success
        assert result.metadata is not None
        assert result.metadata.get("returncode") != 0

    def test_execute_shell_default_timeout(self, shell_tool, workspace):
        ctx = ToolContext(workspace=str(workspace))

        result = shell_tool.execute({"command": "echo ok"}, ctx)

        assert result.success
        assert result.output.strip() == "ok"

    def test_execute_shell_command_not_found(self, shell_tool, workspace):
        ctx = ToolContext(workspace=str(workspace))

        result = shell_tool.execute({"command": "not_exist_cmd_12345"}, ctx)

        assert not result.success

    def test_execute_shell_outside_workspace_blocked(self, shell_tool, workspace):
        ctx = ToolContext(workspace=str(workspace))

        result = shell_tool.execute({"command": "cat ../secret.txt"}, ctx)

        assert not result.success
        assert "forbidden" in result.error.lower()


@pytest.fixture
def web_tools(isolated_registry):
    """提供网络工具实例并注册到隔离注册表。"""
    from agent.tools import register_tool
    from agent.tools.fetch_url import FetchUrlTool
    from agent.tools.web_search import WebSearchTool

    web_search_tool = WebSearchTool()
    fetch_url_tool = FetchUrlTool()
    register_tool(web_search_tool)
    register_tool(fetch_url_tool)
    return web_search_tool, fetch_url_tool


class TestWebSearch:
    def test_web_search_success(self, web_tools, workspace, monkeypatch):
        web_search_tool, _ = web_tools
        ctx = ToolContext(workspace=str(workspace))

        class DummyResult:
            def __init__(self):
                self._results = [
                    {
                        "title": "Python",
                        "href": "https://python.org",
                        "body": "Python is a programming language.",
                    },
                    {
                        "title": "DuckDuckGo",
                        "href": "https://duckduckgo.com",
                        "body": "Privacy-focused search engine.",
                    },
                ]

            def text(self, keywords, max_results=5):
                return iter(self._results)

        monkeypatch.setattr("agent.tools.web_search.DDGS", lambda *args, **kwargs: DummyResult())

        result = web_search_tool.execute({"query": "python"}, ctx)

        assert result.success
        assert "Python" in result.output
        assert "https://python.org" in result.output
        assert result.metadata is not None
        assert len(result.metadata.get("results", [])) == 2

    def test_web_search_failure_returns_empty(self, web_tools, workspace, monkeypatch):
        web_search_tool, _ = web_tools
        ctx = ToolContext(workspace=str(workspace))

        class BrokenDDGS:
            def text(self, keywords, max_results=5):
                raise RuntimeError("network error")

        monkeypatch.setattr("agent.tools.web_search.DDGS", lambda *args, **kwargs: BrokenDDGS())

        result = web_search_tool.execute({"query": "python"}, ctx)

        assert not result.success
        assert result.output == ""
        assert "network error" in result.error
        assert result.metadata is not None
        assert result.metadata.get("results") == []

    def test_web_search_limits_results(self, web_tools, workspace, monkeypatch):
        web_search_tool, _ = web_tools
        ctx = ToolContext(workspace=str(workspace))

        class DummyResult:
            def text(self, keywords, max_results=5):
                assert max_results == 2
                return iter(
                    [
                        {"title": "A", "href": "https://a.com", "body": "a"},
                        {"title": "B", "href": "https://b.com", "body": "b"},
                    ]
                )

        monkeypatch.setattr("agent.tools.web_search.DDGS", lambda *args, **kwargs: DummyResult())

        result = web_search_tool.execute({"query": "test", "max_results": 2}, ctx)

        assert result.success
        assert result.metadata is not None
        assert len(result.metadata.get("results", [])) == 2

    def test_web_search_empty_query(self, web_tools, workspace):
        web_search_tool, _ = web_tools
        ctx = ToolContext(workspace=str(workspace))

        result = web_search_tool.execute({"query": ""}, ctx)

        assert not result.success
        assert "empty" in result.error.lower()
        assert result.output == ""
        assert result.metadata is not None
        assert result.metadata.get("results") == []

    def test_web_search_truncation(self, web_tools, workspace, monkeypatch):
        web_search_tool, _ = web_tools
        ctx = ToolContext(workspace=str(workspace))

        long_body = "x" * 2000

        class DummyResult:
            def text(self, keywords, max_results=5):
                return iter(
                    [
                        {
                            "title": f"Title {i}",
                            "href": f"https://example{i}.com",
                            "body": long_body,
                        }
                        for i in range(5)
                    ]
                )

        monkeypatch.setattr("agent.tools.web_search.DDGS", lambda *args, **kwargs: DummyResult())

        result = web_search_tool.execute({"query": "test"}, ctx)

        assert result.success
        assert len(result.output) == 5000
        assert result.metadata is not None
        assert result.metadata.get("truncated") is True
        assert result.metadata.get("original_length") > 5000
        assert result.metadata.get("count") == 5


class TestFetchUrl:
    def test_fetch_url_success(self, web_tools, workspace, monkeypatch):
        _, fetch_url_tool = web_tools
        ctx = ToolContext(workspace=str(workspace))

        class DummyResponse:
            text = "Hello, world!"
            status_code = 200

            def raise_for_status(self):
                pass

        def fake_get(url, timeout=10):
            return DummyResponse()

        monkeypatch.setattr("agent.tools.fetch_url.requests.get", fake_get)

        result = fetch_url_tool.execute({"url": "https://example.com"}, ctx)

        assert result.success
        assert result.output == "Hello, world!"

    def test_fetch_url_timeout_param(self, web_tools, workspace, monkeypatch):
        _, fetch_url_tool = web_tools
        ctx = ToolContext(workspace=str(workspace))

        class DummyResponse:
            text = "ok"
            status_code = 200

            def raise_for_status(self):
                pass

        captured = {}

        def fake_get(url, timeout):
            captured["timeout"] = timeout
            return DummyResponse()

        monkeypatch.setattr("agent.tools.fetch_url.requests.get", fake_get)

        result = fetch_url_tool.execute({"url": "https://example.com", "timeout": 3}, ctx)

        assert result.success
        assert result.output == "ok"
        assert captured.get("timeout") == 3

    def test_fetch_url_truncation(self, web_tools, workspace, monkeypatch):
        _, fetch_url_tool = web_tools
        ctx = ToolContext(workspace=str(workspace))

        class DummyResponse:
            text = "x" * 6000
            status_code = 200

            def raise_for_status(self):
                pass

        monkeypatch.setattr(
            "agent.tools.fetch_url.requests.get",
            lambda url, timeout=10: DummyResponse(),
        )

        result = fetch_url_tool.execute({"url": "https://example.com"}, ctx)

        assert result.success
        assert len(result.output) == 5000
        assert result.metadata is not None
        assert result.metadata.get("truncated") is True
        assert result.metadata.get("original_length") == 6000

    def test_fetch_url_failure(self, web_tools, workspace, monkeypatch):
        _, fetch_url_tool = web_tools
        ctx = ToolContext(workspace=str(workspace))

        def fake_get(url, timeout=10):
            raise ConnectionError("connection refused")

        monkeypatch.setattr("agent.tools.fetch_url.requests.get", fake_get)

        result = fetch_url_tool.execute({"url": "https://example.com"}, ctx)

        assert not result.success
        assert "connection refused" in result.error

    def test_fetch_url_default_timeout(self, web_tools, workspace, monkeypatch):
        _, fetch_url_tool = web_tools
        ctx = ToolContext(workspace=str(workspace))

        class DummyResponse:
            text = "ok"
            status_code = 200

            def raise_for_status(self):
                pass

        captured = {}

        def fake_get(url, timeout):
            captured["timeout"] = timeout
            return DummyResponse()

        monkeypatch.setattr("agent.tools.fetch_url.requests.get", fake_get)

        result = fetch_url_tool.execute({"url": "https://example.com"}, ctx)

        assert result.success
        assert result.output == "ok"
        assert captured.get("timeout") == 10


@pytest.fixture
def interactive_tools(isolated_registry):
    """提供交互工具实例并注册到隔离注册表。"""
    from agent.tools import register_tool
    from agent.tools.ask_user import AskUserTool
    from agent.tools.set_todo import SetTodoTool

    ask_tool = AskUserTool()
    todo_tool = SetTodoTool()
    register_tool(ask_tool)
    register_tool(todo_tool)
    return ask_tool, todo_tool


class TestAskUser:
    def test_ask_user_without_options(self, interactive_tools, workspace):
        ask_tool, _ = interactive_tools
        ctx = ToolContext(workspace=str(workspace))

        result = ask_tool.execute({"question": "你好吗？"}, ctx)

        assert result.success
        assert "你好吗？" in result.output
        assert "直接回复" in result.output

    def test_ask_user_with_options(self, interactive_tools, workspace):
        ask_tool, _ = interactive_tools
        ctx = ToolContext(workspace=str(workspace))

        result = ask_tool.execute(
            {"question": "选择颜色", "options": ["红色", "绿色", "蓝色"]}, ctx
        )

        assert result.success
        assert "选择颜色" in result.output
        assert "1. 红色" in result.output
        assert "2. 绿色" in result.output
        assert "3. 蓝色" in result.output


class TestSetTodo:
    def test_set_todo_create(self, interactive_tools, workspace, tmp_path):
        _, todo_tool = interactive_tools
        db_path = tmp_path / "todos.db"
        ctx = ToolContext(workspace=str(workspace), db_path=str(db_path))

        result = todo_tool.execute({"action": "create", "id": "todo-1", "title": "实现功能"}, ctx)

        assert result.success
        assert "创建" in result.output
        assert "todo-1" in result.output
        assert "实现功能" in result.output

        mgr = HistoryManager(str(db_path))
        todos = mgr.list_todos(mgr.get_or_create_session(str(workspace)))
        assert any(t["id"] == "todo-1" and t["title"] == "实现功能" for t in todos)

    def test_set_todo_update(self, interactive_tools, workspace, tmp_path):
        _, todo_tool = interactive_tools
        db_path = tmp_path / "todos.db"
        ctx = ToolContext(workspace=str(workspace), db_path=str(db_path))
        todo_tool.execute({"action": "create", "id": "todo-1", "title": "原标题"}, ctx)

        result = todo_tool.execute(
            {"action": "update", "id": "todo-1", "status": "in_progress"}, ctx
        )

        assert result.success
        assert "更新" in result.output
        assert "todo-1" in result.output

        mgr = HistoryManager(str(db_path))
        todos = mgr.list_todos(mgr.get_or_create_session(str(workspace)))
        assert any(t["id"] == "todo-1" and t["status"] == "in_progress" for t in todos)

    def test_set_todo_complete(self, interactive_tools, workspace, tmp_path):
        _, todo_tool = interactive_tools
        db_path = tmp_path / "todos.db"
        ctx = ToolContext(workspace=str(workspace), db_path=str(db_path))
        todo_tool.execute({"action": "create", "id": "todo-1", "title": "待完成"}, ctx)

        result = todo_tool.execute({"action": "complete", "id": "todo-1"}, ctx)

        assert result.success
        assert "完成" in result.output
        assert "todo-1" in result.output

        mgr = HistoryManager(str(db_path))
        todos = mgr.list_todos(mgr.get_or_create_session(str(workspace)))
        assert any(t["id"] == "todo-1" and t["status"] == "done" for t in todos)

    def test_set_todo_list(self, interactive_tools, workspace, tmp_path):
        _, todo_tool = interactive_tools
        db_path = tmp_path / "todos.db"
        ctx = ToolContext(workspace=str(workspace), db_path=str(db_path))
        todo_tool.execute({"action": "create", "id": "todo-1", "title": "任务一"}, ctx)
        todo_tool.execute({"action": "create", "id": "todo-2", "title": "任务二"}, ctx)

        result = todo_tool.execute({"action": "list"}, ctx)

        assert result.success
        assert "todo-1" in result.output
        assert "任务一" in result.output
        assert "todo-2" in result.output
        assert "任务二" in result.output

    def test_set_todo_invalid_action(self, interactive_tools, workspace):
        _, todo_tool = interactive_tools
        ctx = ToolContext(workspace=str(workspace))

        with pytest.raises(ValidationError):
            todo_tool.execute({"action": "invalid_action"}, ctx)
