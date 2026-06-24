import pytest

from agent.tools import ToolContext, get_tool


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path))


def test_read_multiple_files_success(ctx, tmp_path):
    (tmp_path / "a.py").write_text("hello", encoding="utf-8")
    (tmp_path / "b.py").write_text("world", encoding="utf-8")

    tool = get_tool("read_multiple_files")
    result = tool.execute({"paths": ["a.py", "b.py"]}, ctx)

    assert result.success
    assert "a.py" in result.output
    assert "hello" in result.output
    assert "b.py" in result.output
    assert "world" in result.output


def test_read_multiple_files_missing_file(ctx, tmp_path):
    (tmp_path / "a.py").write_text("hello", encoding="utf-8")

    tool = get_tool("read_multiple_files")
    result = tool.execute({"paths": ["a.py", "missing.py"]}, ctx)

    # A missing file no longer aborts the whole batch; the readable files are
    # still returned and the missing one is noted inline.
    assert result.success
    assert "hello" in result.output
    assert "missing.py" in result.output
    assert "error" in result.output.lower()
