import pytest

from agent.indexing import Indexer
from agent.tools import ToolContext, get_tool


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path), db_path=str(tmp_path / "index.db"))


def test_symbol_search(tmp_path, ctx):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    Indexer(str(tmp_path), str(ctx.db_path)).build()

    tool = get_tool("symbol_search")
    result = tool.execute({"query": "add"}, ctx)

    assert result.success
    assert "add" in result.output
