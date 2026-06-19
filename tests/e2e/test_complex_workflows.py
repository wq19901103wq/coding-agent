"""复杂端到端工作流测试。

这些测试模拟真实使用场景，涉及多个工具的串联调用、
REPL 交互、历史持久化和安全确认流程。
"""

import io
from pathlib import Path
from typing import Any

from rich.console import Console

from agent.config import Config, LLMConfig
from agent.history import HistoryManager
from agent.indexing import Indexer
from agent.llm.schema import AssistantResponse, ToolCall
from agent.repl import REPL
from agent.tools import ToolContext, get_tool
from tests.conftest import MockLLM


def _make_config(tmp_path: Path, **overrides: Any) -> Config:
    defaults = {
        "llm": LLMConfig(api_key="test-key", max_steps_per_turn=10),
        "history": {"enabled": True, "db_path": str(tmp_path / "history.db")},
        "security": {
            "confirm_dangerous": True,
            "log_safety_events": False,
            "allow_outside_workspace": False,
        },
    }
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _make_repl(
    tmp_path: Path,
    inputs: list[str],
    llm: MockLLM | None = None,
    config: Config | None = None,
) -> tuple[REPL, io.StringIO]:
    config = config or _make_config(tmp_path)
    input_iter = iter(inputs)

    def input_func(prompt: str = "") -> str:
        return next(input_iter)

    output = io.StringIO()
    console = Console(file=output, color_system=None)
    repl = REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=llm,  # type: ignore[arg-type]
        console=console,
        input_func=input_func,
    )
    return repl, output


# ---------------------------------------------------------------------------
# 测试 1：跨文件重构后索引一致性
# ---------------------------------------------------------------------------


def test_refactor_and_index_consistency(tmp_path):
    """重命名跨文件函数后，重建索引并验证符号一致性。"""
    (tmp_path / "utils.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from utils import helper\nprint(helper())\n", encoding="utf-8"
    )

    db_path = tmp_path / "index.db"
    Indexer(str(tmp_path), str(db_path)).build()

    ctx = ToolContext(workspace=str(tmp_path), db_path=str(db_path))

    # 确认旧符号存在
    old = get_tool("symbol_search").execute({"query": "helper"}, ctx)
    assert old.success
    assert "helper" in old.output

    # 跨文件重命名
    diff = """\
--- utils.py
+++ utils.py
@@ -1,2 +1,2 @@
-def helper():
+def utility():
     return 1
--- main.py
+++ main.py
@@ -1,2 +1,2 @@
-from utils import helper
-print(helper())
+from utils import utility
+print(utility())
"""
    patch_result = get_tool("apply_patch").execute({"diff": diff}, ctx)
    assert patch_result.success

    # 重建索引
    Indexer(str(tmp_path), str(db_path)).build()

    # 验证旧符号消失、新符号存在
    old_after = get_tool("symbol_search").execute({"query": "helper"}, ctx)
    new_after = get_tool("symbol_search").execute({"query": "utility"}, ctx)
    assert "No symbols found" in old_after.output
    assert "utility" in new_after.output


# ---------------------------------------------------------------------------
# 测试 2：完整 bug 修复工作流
# ---------------------------------------------------------------------------


def test_full_bug_fix_workflow(tmp_path):
    """从失败测试开始，定位、读取、修复并验证通过。"""
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "index.db"
    Indexer(str(tmp_path), str(db_path)).build()
    ctx = ToolContext(workspace=str(tmp_path), db_path=str(db_path))

    # 1. 定位符号
    def_result = get_tool("find_definition").execute({"name": "add"}, ctx)
    assert def_result.success
    assert "calc.py" in def_result.output

    # 2. 读取文件
    read_result = get_tool("read_file").execute({"path": "calc.py"}, ctx)
    assert read_result.success
    assert "return a - b" in read_result.output

    # 3. 修复 bug
    diff = """\
--- calc.py
+++ calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""
    patch_result = get_tool("apply_patch").execute({"diff": diff}, ctx)
    assert patch_result.success

    # 4. 运行测试（e2e 中绕过 dangerous 确认）
    shell_result = get_tool("execute_shell").execute_forced(
        {"command": f"cd {tmp_path} && python -m pytest test_calc.py -q"},
        ctx,
    )
    assert shell_result.success
    assert "1 passed" in shell_result.output


# ---------------------------------------------------------------------------
# 测试 3：危险操作拒绝与恢复
# ---------------------------------------------------------------------------


def test_dangerous_patch_decline_then_approve(tmp_path):
    """用户先拒绝 apply_patch，再同意后成功应用。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    decline_call = ToolCall(
        id="1",
        name="apply_patch",
        arguments={"diff": "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-x = 1\n+x = 99\n"},
    )
    approve_call = ToolCall(
        id="2",
        name="apply_patch",
        arguments={"diff": "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"},
    )

    # 场景 1：用户拒绝
    llm_decline = MockLLM(
        responses=[
            AssistantResponse(content="", tool_calls=[decline_call]),
            AssistantResponse(content="已取消"),
        ]
    )
    repl_decline, _ = _make_repl(tmp_path, inputs=["n"], llm=llm_decline)
    repl_decline._run_turn()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"

    # 场景 2：用户同意
    llm_approve = MockLLM(
        responses=[
            AssistantResponse(content="", tool_calls=[approve_call]),
            AssistantResponse(content="完成"),
        ]
    )
    repl_approve, _ = _make_repl(tmp_path, inputs=["y"], llm=llm_approve)
    repl_approve._run_turn()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"


# ---------------------------------------------------------------------------
# 测试 4：待办驱动的多文件任务
# ---------------------------------------------------------------------------


def test_todo_driven_multi_file_task(tmp_path):
    """创建待办、完成跨文件修改、标记待办完成并验证。"""
    db_path = tmp_path / "index.db"
    ctx = ToolContext(workspace=str(tmp_path), db_path=str(db_path))

    # 1. 创建待办
    todo_result = get_tool("set_todo").execute({"action": "create", "title": "重构 old_name"}, ctx)
    assert todo_result.success
    todo_id = todo_result.output.split("id=")[1].split(")")[0]

    # 2. 创建文件
    (tmp_path / "utils.py").write_text("def old_name():\n    pass\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from utils import old_name\nold_name()\n", encoding="utf-8")

    # 3. 跨文件修改
    diff = """\
--- utils.py
+++ utils.py
@@ -1,2 +1,2 @@
-def old_name():
+def new_name():
     pass
--- main.py
+++ main.py
@@ -1,2 +1,2 @@
-from utils import old_name
-old_name()
+from utils import new_name
+new_name()
"""
    patch_result = get_tool("apply_patch").execute({"diff": diff}, ctx)
    assert patch_result.success

    # 4. 标记待办完成
    complete_result = get_tool("set_todo").execute({"action": "complete", "id": todo_id}, ctx)
    assert complete_result.success

    # 5. 验证待办列表
    list_result = get_tool("set_todo").execute({"action": "list"}, ctx)
    assert list_result.success
    assert "done" in list_result.output


# ---------------------------------------------------------------------------
# 测试 5：Patch 失败后成功恢复
# ---------------------------------------------------------------------------


def test_patch_failure_rollback_then_success(tmp_path):
    """错误 patch 触发回滚，正确 patch 最终成功。"""
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    ctx = ToolContext(workspace=str(tmp_path))

    # 错误的 patch：不匹配当前内容
    bad_diff = """\
--- calc.py
+++ calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return WRONG
+    return a + b
"""
    bad_result = get_tool("apply_patch").execute({"diff": bad_diff}, ctx)
    assert not bad_result.success

    # 文件应保持不变
    original = (tmp_path / "calc.py").read_text(encoding="utf-8")
    assert "return a + b" in original
    assert "WRONG" not in original

    # 正确的 patch
    good_diff = """\
--- calc.py
+++ calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a + b + 1
"""
    good_result = get_tool("apply_patch").execute({"diff": good_diff}, ctx)
    assert good_result.success

    updated = (tmp_path / "calc.py").read_text(encoding="utf-8")
    assert "return a + b + 1" in updated


# ---------------------------------------------------------------------------
# 测试 6：REPL 重启后历史恢复
# ---------------------------------------------------------------------------


def test_repl_restart_preserves_history(tmp_path):
    """REPL 执行一轮后重启，验证历史消息恢复。"""
    db_path = tmp_path / "history.db"
    config = _make_config(tmp_path)

    call = ToolCall(
        id="1",
        name="execute_shell",
        arguments={"command": "ls"},
    )
    llm = MockLLM(
        responses=[
            AssistantResponse(content="", tool_calls=[call]),
            AssistantResponse(content="完成"),
        ]
    )

    # 第一次启动并执行一轮
    repl1, _ = _make_repl(tmp_path, inputs=[], llm=llm, config=config)
    repl1._run_turn()

    # 验证历史已保存
    history = HistoryManager(str(db_path))
    messages = history.load_messages(repl1.session_id)
    assert any(msg.role == "assistant" for msg in messages)
    assert any(msg.role == "tool" for msg in messages)

    # 第二次启动，使用同一历史数据库
    llm2 = MockLLM(responses=[AssistantResponse(content="继续")])
    repl2, _ = _make_repl(tmp_path, inputs=["继续"], llm=llm2, config=config)

    # 验证消息已恢复（系统消息 + 上一轮消息）
    assert len(repl2.messages) > 1
    assert repl2.messages[0].role == "system"
