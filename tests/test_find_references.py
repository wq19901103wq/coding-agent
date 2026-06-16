import pytest

from agent.indexing import Indexer
from agent.tools import ToolContext, get_tool


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path), db_path=str(tmp_path / "index.db"))


def test_find_references(tmp_path, ctx):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\nprint(add(1, 2))\n",
        encoding="utf-8",
    )
    Indexer(str(tmp_path), str(ctx.db_path)).build()

    tool = get_tool("find_references")
    result = tool.execute({"name": "add"}, ctx)

    assert result.success
    assert result.output.count("add") >= 2
