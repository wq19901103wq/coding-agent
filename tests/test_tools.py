import pytest
from pydantic import BaseModel as PydanticModel

from agent.tools.base import BaseTool, ToolResult, ToolContext


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
    from agent.tools.write_file import WriteFileTool
    from agent.tools.str_replace_file import StrReplaceFileTool

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

        result = write_tool.execute(
            {"path": "a.py", "content": "\ny=2", "append": True}, ctx
        )

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

        result = replace_tool.execute(
            {"path": "a.py", "old_str": "x=1", "new_str": "x=2"}, ctx
        )

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

        result = replace_tool.execute(
            {"path": "a.py", "old_str": "a", "new_str": "b"}, ctx
        )

        assert not result.success
        assert "unique" in result.error.lower()
        assert (workspace / "a.py").read_text(encoding="utf-8") == "aaa"

    def test_str_replace_outside_workspace(self, file_tools, workspace):
        _, _, replace_tool = file_tools
        ctx = ToolContext(workspace=str(workspace))

        result = replace_tool.execute(
            {"path": "../x.py", "old_str": "x=1", "new_str": "x=2"}, ctx
        )

        assert not result.success
        assert "Path outside workspace" in result.error
