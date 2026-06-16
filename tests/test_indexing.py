from agent.indexing import Indexer
from agent.indexing.parser import parse_file


def test_parse_file(tmp_path):
    code = """\
def foo(x):
    return x + 1

class Bar:
    def baz(self):
        pass
"""
    path = tmp_path / "sample.py"
    path.write_text(code, encoding="utf-8")

    symbols, refs = parse_file(str(path))
    names = {s.name for s in symbols}
    assert "foo" in names
    assert "Bar" in names
    assert "baz" in names


def test_indexer_build_and_query(tmp_path):
    db_path = tmp_path / "index.db"
    indexer = Indexer(str(tmp_path), str(db_path))

    (tmp_path / "mod.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    indexer.build()

    results = indexer.search_symbols("helper")
    assert len(results) == 1
    assert results[0].name == "helper"
