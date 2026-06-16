from agent.tools import ToolContext, get_tool


def test_rename_function_across_files(tmp_path):
    (tmp_path / "utils.py").write_text("def old_name():\n    return 1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from utils import old_name\nprint(old_name())\n", encoding="utf-8"
    )

    ctx = ToolContext(workspace=str(tmp_path))
    tool = get_tool("apply_patch")

    diff = """\
--- utils.py
+++ utils.py
@@ -1,2 +1,2 @@
-def old_name():
+def new_name():
     return 1
--- main.py
+++ main.py
@@ -1,2 +1,2 @@
-from utils import old_name
-print(old_name())
+from utils import new_name
+print(new_name())
"""
    result = tool.execute({"diff": diff}, ctx)
    assert result.success
    assert "new_name" in (tmp_path / "utils.py").read_text(encoding="utf-8")
    assert "new_name" in (tmp_path / "main.py").read_text(encoding="utf-8")
