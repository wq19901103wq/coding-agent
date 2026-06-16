from agent.indexing import Indexer
from agent.tools import ToolContext, get_tool


def test_find_and_edit_function(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    Indexer(str(tmp_path), str(tmp_path / "index.db")).build()

    ctx = ToolContext(workspace=str(tmp_path), db_path=str(tmp_path / "index.db"))
    search_result = get_tool("find_definition").execute({"name": "add"}, ctx)
    assert search_result.success
    assert "calc.py" in search_result.output

    patch_result = get_tool("apply_patch").execute(
        {
            "diff": """\
--- calc.py
+++ calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a + b + 1
"""
        },
        ctx,
    )
    assert patch_result.success
