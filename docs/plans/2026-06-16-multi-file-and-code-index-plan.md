# 多文件编辑与代码索引实现计划（P2）

> **版本：** 0.2.0  
> **状态：** 进行中  
> **最后更新：** 2026-06-16

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 coding-agent 增加多文件编辑能力和基于 AST 的代码索引/语义搜索能力。

**架构：**
- 多文件编辑引入 `read_multiple_files` 和 `apply_patch` 两个工具；`apply_patch` 使用 unified diff 格式，保证原子性（任一 hunk 失败则整体回滚）。
- 代码索引引入 `agent/indexing/` 模块，使用 tree-sitter 解析 Python AST，将符号和引用持久化到 SQLite，并在 REPL 启动时自动构建/增量更新。
- 语义搜索工具包括 `symbol_search`、`find_definition`、`find_references`，均从索引数据库读取。

**技术栈：** `tree-sitter`、`tree-sitter-python`、`sqlite3`、`difflib`（标准库）

---

## 文件结构

### 新增文件

| 文件 | 职责 |
|---|---|
| `agent/tools/read_multiple_files.py` | 一次读取多个文件的工具 |
| `agent/tools/apply_patch.py` | 解析并应用 unified diff 补丁的工具 |
| `agent/indexing/__init__.py` | 索引模块包入口 |
| `agent/indexing/models.py` | `Symbol`、`Reference` 数据模型 |
| `agent/indexing/parser.py` | tree-sitter AST 解析器 |
| `agent/indexing/indexer.py` | 索引构建、增量更新、查询 |
| `agent/tools/symbol_search.py` | 按名称/类型搜索符号 |
| `agent/tools/find_definition.py` | 查找符号定义 |
| `agent/tools/find_references.py` | 查找符号引用 |
| `tests/test_read_multiple_files.py` | `read_multiple_files` 单元测试 |
| `tests/test_apply_patch.py` | `apply_patch` 单元测试 |
| `tests/test_indexing.py` | 索引模块单元测试 |
| `tests/test_symbol_search.py` | `symbol_search` 单元测试 |
| `tests/test_find_definition.py` | `find_definition` 单元测试 |
| `tests/test_find_references.py` | `find_references` 单元测试 |
| `tests/e2e/test_multi_file_refactor.py` | 跨文件重构端到端测试 |
| `tests/e2e/test_symbol_search_and_edit.py` | 语义搜索后修改端到端测试 |

### 修改文件

| 文件 | 修改内容 |
|---|---|
| `pyproject.toml` | 添加 `tree-sitter`、`tree-sitter-python` 依赖 |
| `agent/tools/__init__.py` | 注册 5 个新工具 |
| `agent/repl.py` | 把 `apply_patch` 加入写操作确认集合；启动时构建索引；新增 `/index` 命令 |
| `README.md` | 更新功能列表和工具说明 |
| `CHANGELOG.md` | 记录 0.2.0 新增功能 |

---

## 任务 1：添加 tree-sitter 依赖

**文件：**
- 修改：`pyproject.toml:26-33`

- [ ] **步骤 1：在 `dependencies` 末尾添加依赖**

```toml
dependencies = [
    "openai>=1.0.0",
    "pydantic>=2.0.0",
    "rich>=13.0.0",
    "ddgs>=3.0.0",
    "requests>=2.30.0",
    "tomli>=2.0.0",
    "tree-sitter>=0.22.0",
    "tree-sitter-python>=0.21.0",
]
```

- [ ] **步骤 2：本地安装验证**

```bash
cd /Users/yihanwang/coding-agent
pip install -e ".[dev]"
python -c "from tree_sitter import Language, Parser; import tree_sitter_python; print('OK')"
```

预期输出：`OK`

- [ ] **步骤 3：Commit**

```bash
git add pyproject.toml
git commit -m "build: add tree-sitter dependencies for code indexing"
```

---

## 任务 2：实现 read_multiple_files 工具

**文件：**
- 创建：`agent/tools/read_multiple_files.py`
- 修改：`agent/tools/__init__.py:8` 附近添加 import 和注册
- 测试：`tests/test_read_multiple_files.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_read_multiple_files.py` 写入：

```python
from pathlib import Path

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

    assert not result.success
    assert "missing.py" in result.error
```

- [ ] **步骤 2：运行测试验证失败**

```bash
cd /Users/yihanwang/coding-agent
pytest tests/test_read_multiple_files.py -v
```

预期：`FAILED`（`read_multiple_files` 未注册或不存在）

- [ ] **步骤 3：实现 read_multiple_files 工具**

创建 `agent/tools/read_multiple_files.py`：

```python
from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 8000


class ReadMultipleFilesInput(BaseModel):
    paths: list[str] = Field(..., description="相对于工作目录的文件路径列表")


class ReadMultipleFilesTool(BaseTool):
    name = "read_multiple_files"
    description = "一次读取多个文件内容，适用于跨文件任务"
    input_schema = ReadMultipleFilesInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        outputs: list[str] = []
        total_length = 0
        truncated = False
        original_length = 0

        for path in input["paths"]:
            try:
                target = validate_path(path, ctx.workspace_path)
            except PathOutsideWorkspaceError as exc:
                return ToolResult(success=False, error=str(exc))

            if not target.exists():
                return ToolResult(success=False, error=f"File not found: {path}")
            if target.is_dir():
                return ToolResult(success=False, error=f"Is a directory: {path}")

            try:
                content = target.read_text(encoding="utf-8")
            except OSError as exc:
                return ToolResult(success=False, error=f"Failed to read {path}: {exc}")

            original_length += len(content)
            if total_length + len(content) > MAX_OUTPUT_LENGTH and not truncated:
                remaining = MAX_OUTPUT_LENGTH - total_length
                content = content[:remaining]
                truncated = True

            outputs.append(f"===== {path} =====\n{content}")
            total_length += len(content)

        metadata: dict | None = None
        if truncated:
            metadata = {"truncated": True, "original_length": original_length}

        return ToolResult(success=True, output="\n\n".join(outputs), metadata=metadata)
```

- [ ] **步骤 4：注册工具**

在 `agent/tools/__init__.py` 第 8 行附近添加：

```python
from agent.tools.read_multiple_files import ReadMultipleFilesTool
```

在 `register_tool(StrReplaceFileTool())` 之后添加：

```python
register_tool(ReadMultipleFilesTool())
```

- [ ] **步骤 5：运行测试验证通过**

```bash
pytest tests/test_read_multiple_files.py -v
```

预期：`2 passed`

- [ ] **步骤 6：Commit**

```bash
git add agent/tools/read_multiple_files.py agent/tools/__init__.py tests/test_read_multiple_files.py
git commit -m "feat: add read_multiple_files tool"
```

---

## 任务 3：实现 apply_patch 工具

**文件：**
- 创建：`agent/tools/apply_patch.py`
- 修改：`agent/tools/__init__.py`
- 测试：`tests/test_apply_patch.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_apply_patch.py` 写入：

```python
from pathlib import Path

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
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_apply_patch.py -v
```

预期：`FAILED`（`apply_patch` 未实现）

- [ ] **步骤 3：实现 apply_patch 工具**

创建 `agent/tools/apply_patch.py`：

```python
from pathlib import Path

from pydantic import BaseModel, Field

from agent.safety import PathOutsideWorkspaceError, validate_path
from agent.tools.base import BaseTool, ToolContext, ToolResult


class ApplyPatchInput(BaseModel):
    diff: str = Field(..., description="unified diff 格式的补丁文本")


class Hunk:
    def __init__(self, old_start: int, old_count: int, new_start: int, new_count: int, lines: list[str]):
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.lines = lines


class FilePatch:
    def __init__(self, old_path: str | None, new_path: str | None, hunks: list[Hunk]):
        self.old_path = old_path
        self.new_path = new_path
        self.hunks = hunks


def parse_diff(diff: str) -> list[FilePatch]:
    """简易 unified diff 解析器。"""
    patches: list[FilePatch] = []
    current_hunks: list[Hunk] = []
    current_old: str | None = None
    current_new: str | None = None
    pending_hunk_lines: list[str] | None = None
    pending_meta: dict | None = None

    def flush_hunk() -> None:
        nonlocal pending_hunk_lines, pending_meta
        if pending_meta is None or pending_hunk_lines is None:
            return
        hunks.append(
            Hunk(
                pending_meta["old_start"],
                pending_meta["old_count"],
                pending_meta["new_start"],
                pending_meta["new_count"],
                pending_hunk_lines,
            )
        )
        pending_hunk_lines = None
        pending_meta = None

    for raw_line in diff.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        if line.startswith("--- "):
            flush_hunk()
            if current_old is not None:
                patches.append(FilePatch(current_old, current_new, current_hunks))
            current_old = line[4:].strip()
            if current_old == "/dev/null":
                current_old = None
            current_hunks = []
            pending_meta = None
        elif line.startswith("+++ "):
            current_new = line[4:].strip()
            if current_new == "/dev/null":
                current_new = None
        elif line.startswith("@@"):
            flush_hunk()
            # 格式：@@ -l,s +l,s @@
            parts = line.split("@@")
            ranges = parts[1].strip()
            old_range, new_range = ranges.split(" ")
            old_start, old_count = _parse_range(old_range)
            new_start, new_count = _parse_range(new_range)
            pending_meta = {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
            }
            pending_hunk_lines = []
        elif pending_hunk_lines is not None:
            pending_hunk_lines.append(line)

    flush_hunk()
    if current_old is not None or current_new is not None:
        patches.append(FilePatch(current_old, current_new, current_hunks))

    return patches


def _parse_range(range_str: str) -> tuple[int, int]:
    sign = range_str[0]
    body = range_str[1:]
    if "," in body:
        start, count = body.split(",", 1)
    else:
        start = body
        count = "1"
    return int(start), int(count)


def apply_hunks(content_lines: list[str], hunks: list[Hunk]) -> list[str]:
    """将 hunks 应用到内容行，任一失败抛异常。"""
    result = list(content_lines)
    offset = 0
    for hunk in hunks:
        start_idx = hunk.old_start - 1 + offset
        if start_idx < 0 or start_idx > len(result):
            raise ValueError(f"Hunk start out of range: {hunk.old_start}")

        old_lines: list[str] = []
        new_lines: list[str] = []
        for line in hunk.lines:
            if line.startswith("-"):
                old_lines.append(line[1:])
            elif line.startswith("+"):
                new_lines.append(line[1:])
            elif line.startswith(" "):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
            elif line.startswith("\\"):
                # "\ No newline at end of file" 忽略
                continue

        # 校验 old_lines 是否匹配
        actual = result[start_idx : start_idx + len(old_lines)]
        if actual != old_lines:
            raise ValueError(
                f"Hunk does not match at line {hunk.old_start}.\n"
                f"Expected:\n{'\\n'.join(old_lines)}\nActual:\n{'\\n'.join(actual)}"
            )

        result[start_idx : start_idx + len(old_lines)] = new_lines
        offset += len(new_lines) - len(old_lines)

    return result


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = "使用 unified diff 同时修改多个文件，支持新增和删除文件"
    input_schema = ApplyPatchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        diff = input["diff"]
        try:
            patches = parse_diff(diff)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse diff: {exc}")

        if not patches:
            return ToolResult(success=False, error="No file patches found in diff")

        # 第一阶段：校验所有路径合法性
        for patch in patches:
            path = patch.new_path or patch.old_path
            if path is None:
                return ToolResult(success=False, error="Patch missing both old and new path")
            try:
                validate_path(path, ctx.workspace_path)
            except PathOutsideWorkspaceError as exc:
                return ToolResult(success=False, error=str(exc))

        # 第二阶段：先全部验证通过后再写入；记录备份用于回滚
        backups: dict[Path, str | None] = {}
        try:
            for patch in patches:
                path = patch.new_path or patch.old_path
                assert path is not None
                target = validate_path(path, ctx.workspace_path)

                if patch.new_path is None:
                    # 删除文件
                    backups[target] = target.read_text(encoding="utf-8") if target.exists() else None
                elif patch.old_path is None or not target.exists():
                    # 新增文件
                    backups[target] = None
                    new_content = apply_hunks([], patch.hunks)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("\n".join(new_content) + "\n", encoding="utf-8")
                else:
                    # 修改文件
                    original = target.read_text(encoding="utf-8")
                    backups[target] = original
                    content_lines = original.splitlines()
                    new_lines = apply_hunks(content_lines, patch.hunks)
                    # 保持末尾换行
                    ending = "\n" if original.endswith("\n") else ""
                    target.write_text("\n".join(new_lines) + ending, encoding="utf-8")
        except Exception as exc:
            # 回滚
            for target, original in backups.items():
                if original is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.write_text(original, encoding="utf-8")
            return ToolResult(success=False, error=f"Failed to apply patch: {exc}")

        affected = [patch.new_path or patch.old_path for patch in patches]
        return ToolResult(
            success=True,
            output=f"Patch applied to {len(affected)} file(s): {', '.join(affected)}",
            metadata={"affected_files": affected},
        )
```

- [ ] **步骤 4：注册工具**

在 `agent/tools/__init__.py` 添加：

```python
from agent.tools.apply_patch import ApplyPatchTool
```

并注册：

```python
register_tool(ApplyPatchTool())
```

- [ ] **步骤 5：运行测试验证通过**

```bash
pytest tests/test_apply_patch.py -v
```

预期：`4 passed`

- [ ] **步骤 6：Commit**

```bash
git add agent/tools/apply_patch.py agent/tools/__init__.py tests/test_apply_patch.py
git commit -m "feat: add apply_patch tool with atomic rollback"
```

---

## 任务 4：REPL 把 apply_patch 标记为写操作

**文件：**
- 修改：`agent/repl.py:28`
- 测试：`tests/test_repl.py` 或新建 `tests/test_repl_safety.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_repl_safety.py` 写入：

```python
from unittest.mock import MagicMock

from agent.repl import REPL


def test_apply_patch_triggers_confirmation(tmp_path):
    config = MagicMock()
    config.security.confirm_dangerous = True
    config.history.enabled = False
    config.llm.max_steps_per_turn = 5
    config.history.db_path = None

    inputs = iter(["n"])

    def fake_input(prompt: str = "") -> str:
        return next(inputs)

    repl = REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=MagicMock(),
        input_func=fake_input,
    )

    from agent.llm.schema import ToolCall

    call = ToolCall(id="1", name="apply_patch", arguments={"diff": "--- a\n+++ a\n@@ -1 +1 @@\n-x\n+y\n"})
    result = repl._execute_tool_call(call)

    assert not result.success
    assert "User declined" in result.error
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_repl_safety.py -v
```

预期：`FAILED`（`apply_patch` 不被视为写操作，不会触发确认）

- [ ] **步骤 3：修改 REPL**

在 `agent/repl.py` 第 28 行：

```python
_FILE_WRITE_TOOLS = {"write_file", "str_replace_file", "apply_patch"}
```

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/test_repl_safety.py -v
```

预期：`1 passed`

- [ ] **步骤 5：Commit**

```bash
git add agent/repl.py tests/test_repl_safety.py
git commit -m "feat: require confirmation before applying patch"
```

---

## 任务 5：实现代码索引模块

**文件：**
- 创建：`agent/indexing/__init__.py`
- 创建：`agent/indexing/models.py`
- 创建：`agent/indexing/parser.py`
- 创建：`agent/indexing/indexer.py`
- 测试：`tests/test_indexing.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_indexing.py` 写入：

```python
from pathlib import Path

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
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_indexing.py -v
```

预期：`FAILED`（模块不存在）

- [ ] **步骤 3：实现 models**

创建 `agent/indexing/models.py`：

```python
from dataclasses import dataclass


@dataclass
class Symbol:
    path: str
    name: str
    kind: str
    line: int
    column: int
    scope: str | None = None
    signature: str | None = None


@dataclass
class Reference:
    path: str
    name: str
    line: int
    column: int
    is_definition: bool = False
```

- [ ] **步骤 4：实现 parser**

创建 `agent/indexing/parser.py`：

```python
import os
from pathlib import Path

from tree_sitter import Language, Parser, Tree

from agent.indexing.models import Reference, Symbol


def get_parser() -> Parser:
    import tree_sitter_python as tspython

    language = Language(tspython.language())
    parser = Parser(language)
    return parser


PYTHON_KIND_MAP = {
    "function_definition": "function",
    "class_definition": "class",
    "method_definition": "method",
}


def _node_kind(node_type: str) -> str | None:
    return PYTHON_KIND_MAP.get(node_type)


def parse_file(file_path: str) -> tuple[list[Symbol], list[Reference]]:
    parser = get_parser()
    source_bytes = Path(file_path).read_bytes()
    tree = parser.parse(source_bytes)

    symbols: list[Symbol] = []
    references: list[Reference] = []
    root = tree.root_node
    rel_path = _relative_path(file_path)

    def visit(node, scope: str | None = None):
        kind = _node_kind(node.type)
        if kind and node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                symbols.append(
                    Symbol(
                        path=rel_path,
                        name=name,
                        kind=kind,
                        line=name_node.start_point[0] + 1,
                        column=name_node.start_point[1],
                        scope=scope,
                    )
                )
                new_scope = f"{scope}.{name}" if scope else name
                for child in node.children:
                    visit(child, new_scope)
                return

        if node.type == "identifier":
            name = node.text.decode("utf-8")
            references.append(
                Reference(
                    path=rel_path,
                    name=name,
                    line=node.start_point[0] + 1,
                    column=node.start_point[1],
                )
            )

        for child in node.children:
            visit(child, scope)

    visit(root)
    return symbols, references


def _relative_path(file_path: str) -> str:
    cwd = Path.cwd()
    try:
        return str(Path(file_path).relative_to(cwd))
    except ValueError:
        return file_path


def parse_workspace(workspace: str) -> tuple[list[Symbol], list[Reference]]:
    all_symbols: list[Symbol] = []
    all_refs: list[Reference] = []
    ws_path = Path(workspace)
    for py_file in ws_path.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        symbols, refs = parse_file(str(py_file))
        all_symbols.extend(symbols)
        all_refs.extend(refs)
    return all_symbols, all_refs
```

- [ ] **步骤 5：实现 indexer**

创建 `agent/indexing/indexer.py`：

```python
import os
import sqlite3
from pathlib import Path

from agent.indexing.models import Reference, Symbol
from agent.indexing.parser import parse_workspace


SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    column INTEGER NOT NULL,
    scope TEXT,
    signature TEXT
);

CREATE TABLE IF NOT EXISTS references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    line INTEGER NOT NULL,
    column INTEGER NOT NULL,
    is_definition INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL
);
"""


class Indexer:
    def __init__(self, workspace: str, db_path: str | None = None):
        self.workspace = str(Path(workspace).resolve())
        self.db_path = db_path or os.path.expanduser("~/.coding-agent/code_index.db")
        self._init_db()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(SCHEMA)

    def build(self) -> None:
        symbols, refs = parse_workspace(self.workspace)
        with self._connection() as conn:
            conn.execute("DELETE FROM symbols WHERE workspace = ?", (self.workspace,))
            conn.execute("DELETE FROM references WHERE workspace = ?", (self.workspace,))
            conn.execute("DELETE FROM files WHERE path LIKE ?", (f"{self.workspace}%",))

            for symbol in symbols:
                conn.execute(
                    """
                    INSERT INTO symbols
                    (workspace, path, name, kind, line, column, scope, signature)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.workspace,
                        symbol.path,
                        symbol.name,
                        symbol.kind,
                        symbol.line,
                        symbol.column,
                        symbol.scope,
                        symbol.signature,
                    ),
                )

            for ref in refs:
                conn.execute(
                    """
                    INSERT INTO references
                    (workspace, path, name, line, column, is_definition)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (self.workspace, ref.path, ref.name, ref.line, ref.column, int(ref.is_definition)),
                )

            for py_file in Path(self.workspace).rglob("*.py"):
                if "__pycache__" in py_file.parts:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO files (path, mtime) VALUES (?, ?)",
                    (str(py_file), py_file.stat().st_mtime),
                )

    def is_stale(self) -> bool:
        with self._connection() as conn:
            stored = {
                row["path"]: row["mtime"]
                for row in conn.execute("SELECT path, mtime FROM files WHERE path LIKE ?", (f"{self.workspace}%",))
            }

        current: dict[str, float] = {}
        for py_file in Path(self.workspace).rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            current[str(py_file)] = py_file.stat().st_mtime

        return stored != current

    def search_symbols(self, query: str, kind: str | None = None) -> list[Symbol]:
        with self._connection() as conn:
            sql = "SELECT * FROM symbols WHERE workspace = ? AND name LIKE ?"
            params: list = [self.workspace, f"%{query}%"]
            if kind:
                sql += " AND kind = ?"
                params.append(kind)
            rows = conn.execute(sql, params).fetchall()

        return [
            Symbol(
                path=row["path"],
                name=row["name"],
                kind=row["kind"],
                line=row["line"],
                column=row["column"],
                scope=row["scope"],
                signature=row["signature"],
            )
            for row in rows
        ]

    def find_definition(self, name: str) -> list[Symbol]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM symbols WHERE workspace = ? AND name = ?",
                (self.workspace, name),
            ).fetchall()
        return [
            Symbol(
                path=row["path"],
                name=row["name"],
                kind=row["kind"],
                line=row["line"],
                column=row["column"],
                scope=row["scope"],
                signature=row["signature"],
            )
            for row in rows
        ]

    def find_references(self, name: str) -> list[Reference]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM references WHERE workspace = ? AND name = ? ORDER BY path, line",
                (self.workspace, name),
            ).fetchall()
        return [
            Reference(
                path=row["path"],
                name=row["name"],
                line=row["line"],
                column=row["column"],
                is_definition=bool(row["is_definition"]),
            )
            for row in rows
        ]
```

- [ ] **步骤 6：实现 `__init__.py`**

创建 `agent/indexing/__init__.py`：

```python
from agent.indexing.indexer import Indexer
from agent.indexing.models import Reference, Symbol

__all__ = ["Indexer", "Reference", "Symbol"]
```

- [ ] **步骤 7：运行测试验证通过**

```bash
pytest tests/test_indexing.py -v
```

预期：`2 passed`

- [ ] **步骤 8：Commit**

```bash
git add agent/indexing/
git add tests/test_indexing.py
git commit -m "feat: add Python AST-based code indexing module"
```

---

## 任务 6：实现 symbol_search 工具

**文件：**
- 创建：`agent/tools/symbol_search.py`
- 修改：`agent/tools/__init__.py`
- 测试：`tests/test_symbol_search.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_symbol_search.py` 写入：

```python
from pathlib import Path

import pytest

from agent.indexing import Indexer
from agent.tools import ToolContext, get_tool


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path))


def test_symbol_search(tmp_path, ctx):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    Indexer(str(tmp_path), str(tmp_path / "index.db")).build()

    tool = get_tool("symbol_search")
    result = tool.execute({"query": "add"}, ctx)

    assert result.success
    assert "add" in result.output
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_symbol_search.py -v
```

预期：`FAILED`

- [ ] **步骤 3：实现 symbol_search 工具**

创建 `agent/tools/symbol_search.py`：

```python
from pydantic import BaseModel, Field

from agent.indexing import Indexer
from agent.tools.base import BaseTool, ToolContext, ToolResult


class SymbolSearchInput(BaseModel):
    query: str = Field(..., description="符号名称或名称片段")
    kind: str | None = Field(default=None, description="可选类型过滤：function/class/method")


class SymbolSearchTool(BaseTool):
    name = "symbol_search"
    description = "按名称搜索代码符号（函数、类、方法）"
    input_schema = SymbolSearchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        db_path = ctx.db_path or "~/.coding-agent/code_index.db"
        indexer = Indexer(ctx.workspace, db_path)
        symbols = indexer.search_symbols(input["query"], input.get("kind"))

        if not symbols:
            return ToolResult(success=True, output="No symbols found.")

        lines = [f"{s.path}:{s.line}:{s.column} [{s.kind}] {s.name}" for s in symbols]
        return ToolResult(success=True, output="\n".join(lines), metadata={"count": len(symbols)})
```

- [ ] **步骤 4：注册工具**

在 `agent/tools/__init__.py` 添加：

```python
from agent.tools.symbol_search import SymbolSearchTool
```

并注册：

```python
register_tool(SymbolSearchTool())
```

- [ ] **步骤 5：运行测试验证通过**

```bash
pytest tests/test_symbol_search.py -v
```

预期：`1 passed`

- [ ] **步骤 6：Commit**

```bash
git add agent/tools/symbol_search.py agent/tools/__init__.py tests/test_symbol_search.py
git commit -m "feat: add symbol_search tool"
```

---

## 任务 7：实现 find_definition 工具

**文件：**
- 创建：`agent/tools/find_definition.py`
- 修改：`agent/tools/__init__.py`
- 测试：`tests/test_find_definition.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_find_definition.py` 写入：

```python
from pathlib import Path

import pytest

from agent.indexing import Indexer
from agent.tools import ToolContext, get_tool


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path))


def test_find_definition(tmp_path, ctx):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    Indexer(str(tmp_path), str(tmp_path / "index.db")).build()

    tool = get_tool("find_definition")
    result = tool.execute({"name": "add"}, ctx)

    assert result.success
    assert "calc.py:1" in result.output
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_find_definition.py -v
```

预期：`FAILED`

- [ ] **步骤 3：实现 find_definition 工具**

创建 `agent/tools/find_definition.py`：

```python
from pydantic import BaseModel, Field

from agent.indexing import Indexer
from agent.tools.base import BaseTool, ToolContext, ToolResult


class FindDefinitionInput(BaseModel):
    name: str = Field(..., description="要查找定义的符号名称")


class FindDefinitionTool(BaseTool):
    name = "find_definition"
    description = "查找符号的定义位置"
    input_schema = FindDefinitionInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        db_path = ctx.db_path or "~/.coding-agent/code_index.db"
        indexer = Indexer(ctx.workspace, db_path)
        symbols = indexer.find_definition(input["name"])

        if not symbols:
            return ToolResult(success=True, output="No definitions found.")

        lines = [f"{s.path}:{s.line}:{s.column} [{s.kind}] {s.name}" for s in symbols]
        return ToolResult(success=True, output="\n".join(lines), metadata={"count": len(symbols)})
```

- [ ] **步骤 4：注册工具**

在 `agent/tools/__init__.py` 添加：

```python
from agent.tools.find_definition import FindDefinitionTool
```

并注册：

```python
register_tool(FindDefinitionTool())
```

- [ ] **步骤 5：运行测试验证通过**

```bash
pytest tests/test_find_definition.py -v
```

预期：`1 passed`

- [ ] **步骤 6：Commit**

```bash
git add agent/tools/find_definition.py agent/tools/__init__.py tests/test_find_definition.py
git commit -m "feat: add find_definition tool"
```

---

## 任务 8：实现 find_references 工具

**文件：**
- 创建：`agent/tools/find_references.py`
- 修改：`agent/tools/__init__.py`
- 测试：`tests/test_find_references.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_find_references.py` 写入：

```python
from pathlib import Path

import pytest

from agent.indexing import Indexer
from agent.tools import ToolContext, get_tool


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path))


def test_find_references(tmp_path, ctx):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\nprint(add(1, 2))\n",
        encoding="utf-8",
    )
    Indexer(str(tmp_path), str(tmp_path / "index.db")).build()

    tool = get_tool("find_references")
    result = tool.execute({"name": "add"}, ctx)

    assert result.success
    assert result.output.count("add") >= 2
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_find_references.py -v
```

预期：`FAILED`

- [ ] **步骤 3：实现 find_references 工具**

创建 `agent/tools/find_references.py`：

```python
from pydantic import BaseModel, Field

from agent.indexing import Indexer
from agent.tools.base import BaseTool, ToolContext, ToolResult


class FindReferencesInput(BaseModel):
    name: str = Field(..., description="要查找引用的符号名称")


class FindReferencesTool(BaseTool):
    name = "find_references"
    description = "查找符号的所有引用位置"
    input_schema = FindReferencesInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        db_path = ctx.db_path or "~/.coding-agent/code_index.db"
        indexer = Indexer(ctx.workspace, db_path)
        refs = indexer.find_references(input["name"])

        if not refs:
            return ToolResult(success=True, output="No references found.")

        lines = [f"{r.path}:{r.line}:{r.column} {'(def)' if r.is_definition else ''}" for r in refs]
        return ToolResult(success=True, output="\n".join(lines), metadata={"count": len(refs)})
```

- [ ] **步骤 4：注册工具**

在 `agent/tools/__init__.py` 添加：

```python
from agent.tools.find_references import FindReferencesTool
```

并注册：

```python
register_tool(FindReferencesTool())
```

- [ ] **步骤 5：运行测试验证通过**

```bash
pytest tests/test_find_references.py -v
```

预期：`1 passed`

- [ ] **步骤 6：Commit**

```bash
git add agent/tools/find_references.py agent/tools/__init__.py tests/test_find_references.py
git commit -m "feat: add find_references tool"
```

---

## 任务 9：REPL 集成索引

**文件：**
- 修改：`agent/repl.py`
- 测试：`tests/test_repl_indexing.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_repl_indexing.py` 写入：

```python
from pathlib import Path
from unittest.mock import MagicMock

from agent.repl import REPL


def test_repl_builds_index(tmp_path):
    (tmp_path / "math.py").write_text("def square(x):\n    return x * x\n", encoding="utf-8")

    config = MagicMock()
    config.security.confirm_dangerous = False
    config.history.enabled = False
    config.llm.max_steps_per_turn = 1
    config.history.db_path = None

    repl = REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=MagicMock(),
        input_func=lambda _: "exit",
    )

    # 索引应在构建时创建
    index_path = Path.home() / ".coding-agent" / "code_index.db"
    assert index_path.exists()
```

- [ ] **步骤 2：运行测试验证失败**

```bash
pytest tests/test_repl_indexing.py -v
```

预期：`FAILED`

- [ ] **步骤 3：修改 REPL 构建索引**

在 `agent/repl.py` 顶部添加导入：

```python
from agent.indexing import Indexer
```

在 `REPL.__init__` 中，在 `self.tools_schema = ...` 之后添加：

```python
        self.indexer = Indexer(self.workspace, self.config.history.db_path)
        if self.indexer.is_stale():
            self.indexer.build()
```

注意：`config.history.db_path` 可能是 `~/.coding-agent/history.db`。若未启用历史，则使用默认索引路径。

更稳妥地：

```python
        index_db_path = self.config.history.db_path or os.path.expanduser("~/.coding-agent/code_index.db")
        self.indexer = Indexer(self.workspace, index_db_path)
        if self.indexer.is_stale():
            self.indexer.build()
```

并在文件顶部添加 `import os`。

- [ ] **步骤 4：添加 /index 命令**

在 `_handle_slash_command` 的 `/model` 分支后添加：

```python
        elif name == "/index":
            self.console.print("[bold blue]正在重建代码索引...[/bold blue]")
            self.indexer.build()
            self.console.print("[bold green]代码索引已重建。[/bold green]")
```

更新 `_print_help`：

```
  /index  重建代码索引
```

- [ ] **步骤 5：运行测试验证通过**

```bash
pytest tests/test_repl_indexing.py -v
```

预期：`1 passed`

- [ ] **步骤 6：Commit**

```bash
git add agent/repl.py tests/test_repl_indexing.py
git commit -m "feat: REPL auto-builds code index and supports /index command"
```

---

## 任务 10：端到端测试

**文件：**
- 创建：`tests/e2e/test_multi_file_refactor.py`
- 创建：`tests/e2e/test_symbol_search_and_edit.py`

- [ ] **步骤 1：实现跨文件重构 E2E 测试**

在 `tests/e2e/test_multi_file_refactor.py` 写入：

```python
from pathlib import Path

from agent.tools import ToolContext, get_tool


def test_rename_function_across_files(tmp_path):
    (tmp_path / "utils.py").write_text("def old_name():\n    return 1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from utils import old_name\nprint(old_name())\n", encoding="utf-8")

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
```

- [ ] **步骤 2：实现语义搜索后修改 E2E 测试**

在 `tests/e2e/test_symbol_search_and_edit.py` 写入：

```python
from pathlib import Path

from agent.indexing import Indexer
from agent.tools import ToolContext, get_tool


def test_find_and_edit_function(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    Indexer(str(tmp_path), str(tmp_path / "index.db")).build()

    ctx = ToolContext(workspace=str(tmp_path))
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
```

- [ ] **步骤 3：运行测试验证通过**

```bash
pytest tests/e2e/test_multi_file_refactor.py tests/e2e/test_symbol_search_and_edit.py -v
```

预期：`2 passed`

- [ ] **步骤 4：Commit**

```bash
git add tests/e2e/
git commit -m "test: add e2e tests for multi-file refactor and symbol search workflow"
```

---

## 任务 11：全量验证与文档更新

**文件：**
- 修改：`README.md`
- 修改：`CHANGELOG.md`

- [ ] **步骤 1：全量测试**

```bash
cd /Users/yihanwang/coding-agent
pytest -q
mypy agent tests
ruff format --check
ruff check
```

预期：
- `pytest`：所有测试通过（数量 >= 之前的 189 + 新增测试）
- `mypy agent tests`：无错误
- `ruff format --check`：无格式问题
- `ruff check`：无 lint 问题

- [ ] **步骤 2：修复 mypy 问题**

如果 `tree_sitter` 包没有类型存根，在 `pyproject.toml` 的 `[tool.mypy]` 段添加：

```toml
[[tool.mypy.overrides]]
module = ["tree_sitter", "tree_sitter_python"]
follow_untyped_imports = true
```

- [ ] **步骤 3：更新 README**

在 `README.md` 的功能列表中新增：

```markdown
### 多文件编辑
- `read_multiple_files`：一次读取多个文件
- `apply_patch`：使用 unified diff 安全地批量修改多个文件

### 代码索引与语义搜索
- `symbol_search`：按名称搜索函数、类、方法
- `find_definition`：跳转到符号定义
- `find_references`：查找符号引用
```

- [ ] **步骤 4：更新 CHANGELOG**

在 `CHANGELOG.md` 顶部添加：

```markdown
## [0.2.0] - 2026-06-16

### Added
- 多文件编辑工具 `read_multiple_files` 和 `apply_patch`
- 基于 AST 的代码索引，支持自动构建和增量更新
- 语义搜索工具 `symbol_search`、`find_definition`、`find_references`
- REPL `/index` 命令用于手动重建索引
```

- [ ] **步骤 5：最终全量验证**

```bash
pytest -q
mypy agent tests
ruff format --check
ruff check
```

预期：全部通过

- [ ] **步骤 6：Commit**

```bash
git add README.md CHANGELOG.md pyproject.toml
git commit -m "docs: update README and CHANGELOG for 0.2.0"
```

---

## 自检

**1. 规格覆盖度：**

| 规格需求 | 对应任务 |
|---|---|
| 多文件读取 | 任务 2 |
| unified diff 批量修改 | 任务 3 |
| 原子性回滚 | 任务 3 测试 |
| 路径安全校验 | 任务 3、任务 4 |
| AST 索引 | 任务 5 |
| symbol_search | 任务 6 |
| find_definition | 任务 7 |
| find_references | 任务 8 |
| REPL 自动构建索引 | 任务 9 |
| /index 命令 | 任务 9 |
| 端到端测试 | 任务 10 |
| 文档更新 | 任务 11 |

**2. 占位符扫描：** 本计划无 "TODO"、"待定"、"后续实现" 等占位符；每个代码步骤均包含完整代码。

**3. 类型一致性：**
- `ToolContext.workspace_path` 是 `Path` 属性（已有）
- `Indexer` 的 `db_path` 参数统一为 `str | None`
- 工具输入统一继承 `BaseModel`

---

## 执行交接

**计划已完成并保存到 `docs/plans/2026-06-16-multi-file-and-code-index-plan.md`。两种执行方式：**

**1. 子代理驱动（推荐）** - 每个任务调度一个新的子代理，任务间进行审查，快速迭代

**2. 内联执行** - 在当前会话中使用 `superpowers:executing-plans` 执行任务，批量执行并设有检查点供审查

**选哪种方式？**
