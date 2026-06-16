import pytest

from agent.tools import ToolContext, get_tool


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path))


def test_apply_patch_single_file(ctx, tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")

    diff = """\
--- a.py
+++ a.py
@@ -1,2 +1,2 @@
 def foo():
-    pass
+    return 1
"""
    tool = get_tool("apply_patch")
    result = tool.execute({"diff": diff}, ctx)

    assert result.success
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "def foo():\n    return 1\n"


def test_apply_patch_multi_file(ctx, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")

    diff = """\
--- a.py
+++ a.py
@@ -1 +1 @@
-x = 1
+x = 10
--- b.py
+++ b.py
@@ -1 +1 @@
-y = 2
+y = 20
"""
    tool = get_tool("apply_patch")
    result = tool.execute({"diff": diff}, ctx)

    assert result.success
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 10\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "y = 20\n"


def test_apply_patch_new_file(ctx, tmp_path):
    diff = """\
--- /dev/null
+++ c.py
@@ -0,0 +1 @@
+z = 3
"""
    tool = get_tool("apply_patch")
    result = tool.execute({"diff": diff}, ctx)

    assert result.success
    assert (tmp_path / "c.py").read_text(encoding="utf-8") == "z = 3\n"


def test_apply_patch_atomic_rollback(ctx, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")

    diff = """\
--- a.py
+++ a.py
@@ -1 +1 @@
-x = 1
+x = 10
--- b.py
+++ b.py
@@ -1 +1 @@
-y = NONEXISTENT
+y = 20
"""
    tool = get_tool("apply_patch")
    result = tool.execute({"diff": diff}, ctx)

    assert not result.success
    # 失败时整体回滚
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "y = 2\n"
