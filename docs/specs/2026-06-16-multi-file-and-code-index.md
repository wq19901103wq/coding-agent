# coding-agent 多文件编辑与代码索引设计

## 1. 背景

当前 coding-agent 首期 MVP 限制为单文件或少量文件（3-5 个）任务，搜索工具也只有纯文本 grep。真实项目开发中，绝大多数任务需要跨文件协作（如修改 model 后同步改 view、serializer、test），且需要基于代码结构（而非文本）查找符号。

本设计引入两大能力：

1. **多文件编辑**：支持一次 turn 内读取、修改多个文件，并提供变更预览（diff）。
2. **代码索引与语义搜索**：基于 AST 建立项目级符号索引，支持定义跳转、引用查找、符号搜索。

## 2. 目标

- 支持跨文件重构，不限制文件数量（受 `max_steps_per_turn` 约束）。
- 提供 `apply_patch` 工具，使用 unified diff 格式安全地修改多个文件。
- 提供 `read_multiple_files` 工具，一次读取多个文件。
- 使用 tree-sitter 解析 Python 代码，建立符号索引。
- 提供 `symbol_search`、`find_definition`、`find_references` 三个语义搜索工具。
- 保持现有安全策略：写操作需要用户确认，路径越界禁止。

## 3. 术语表

| 术语 | 说明 |
|---|---|
| Patch / Diff | unified diff 格式，描述文件变更 |
| Symbol | 函数、类、方法、变量等代码符号 |
| AST | 抽象语法树 |
| Index | 项目级符号索引数据库 |
| Reference | 某符号被使用的位置 |
| Definition | 某符号被定义的位置 |

## 4. 多文件编辑

### 4.1 新增工具

#### `read_multiple_files`

```python
class ReadMultipleFilesInput(BaseModel):
    paths: list[str] = Field(..., description="相对于工作目录的文件路径列表")
```

- 一次读取多个文件，返回合并后的内容
- 每个文件之间用分隔线区分
- 超长自动截断

#### `apply_patch`

```python
class ApplyPatchInput(BaseModel):
    diff: str = Field(..., description="unified diff 格式的补丁文本")
```

- 解析 diff，应用到多个文件
- 支持新增、删除、修改文件
- 每个文件的修改必须唯一匹配（类似 `str_replace_file` 的约束）
- 如果某个 hunks 匹配失败，整个 patch 回滚，返回错误

### 4.2 安全策略

- `apply_patch` 属于写操作，需要用户确认
- 确认前展示 diff 摘要（涉及哪些文件、多少处变更）
- 所有路径解析后必须在工作目录内

### 4.3 REPL 交互

- LLM 调用 `apply_patch` 时，REPL 先展示变更概览
- 用户输入 `y` 应用，`n` 拒绝，`a` 后续同类操作不再询问
- 应用成功后，可展示最终 diff

### 4.4 示例

用户输入：

```
把项目中所有 print 语句改成 logging.info
```

Agent 流程：

1. `code_search` 找到所有 `print(`
2. LLM 生成 patch
3. `apply_patch` 应用到多个文件
4. 完成

## 5. 代码索引与语义搜索

### 5.1 索引方案

使用 `tree-sitter` 解析 Python 代码，建立 SQLite 索引：`code_index.db`。

#### 索引内容

**symbols 表**

```sql
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,  -- function, class, method, variable, import
    line INTEGER NOT NULL,
    column INTEGER NOT NULL,
    scope TEXT,          -- 父级符号，如 MyClass.method
    signature TEXT       -- 函数签名等附加信息
);
```

**references 表**

```sql
CREATE TABLE references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    line INTEGER NOT NULL,
    column INTEGER NOT NULL,
    is_definition BOOLEAN NOT NULL DEFAULT 0
);
```

### 5.2 索引生命周期

- REPL 启动时，检查索引是否存在或是否需要更新
- 提供 `/index` 命令手动重建索引
- 文件被修改后，增量更新索引

### 5.3 新增工具

#### `symbol_search`

```python
class SymbolSearchInput(BaseModel):
    query: str = Field(..., description="符号名称或通配符")
    kind: str | None = Field(default=None, description="符号类型：function/class/method/variable")
```

返回匹配的符号列表：`path:line:kind name`。

#### `find_definition`

```python
class FindDefinitionInput(BaseModel):
    name: str = Field(..., description="符号名称")
    path: str = Field(default=".", description="搜索起点文件或目录")
```

返回某符号的定义位置。

#### `find_references`

```python
class FindReferencesInput(BaseModel):
    name: str = Field(..., description="符号名称")
    path: str = Field(default=".", description="搜索起点文件或目录")
```

返回某符号的所有引用位置。

### 5.4 示例

用户输入：

```
UserService 在哪里被调用？
```

Agent 流程：

1. `find_references` 查找 `UserService`
2. 读取关键调用位置
3. 给出总结

## 6. 项目结构更新

```
coding-agent/
├── agent/
│   ├── indexing/
│   │   ├── __init__.py
│   │   ├── parser.py        # tree-sitter 解析
│   │   ├── indexer.py       # 索引构建与更新
│   │   └── models.py        # Symbol, Reference 模型
│   ├── tools/
│   │   ├── read_multiple_files.py
│   │   ├── apply_patch.py
│   │   ├── symbol_search.py
│   │   ├── find_definition.py
│   │   └── find_references.py
│   └── ...
├── tests/
│   ├── test_indexing.py
│   └── test_multi_file.py
└── docs/specs/...
```

## 7. 技术栈

- `tree-sitter` + `tree-sitter-python`：Python AST 解析
- `difflib` / `patch`：diff 解析与应用
- `sqlite3`：索引持久化

## 8. 实现顺序

1. 引入 `tree-sitter` 依赖
2. 实现 `ReadMultipleFilesTool`
3. 实现 `ApplyPatchTool`
4. 实现索引模块（parser + indexer）
5. 实现 `SymbolSearchTool`、`FindDefinitionTool`、`FindReferencesTool`
6. 更新 REPL，支持 apply_patch 的批量确认和 diff 展示
7. 更新安全策略，把 apply_patch 列为 dangerous
8. 更新测试

## 9. 验收标准

- [ ] `read_multiple_files` 能一次读取 3 个以上文件
- [ ] `apply_patch` 能正确应用 unified diff 到多个文件
- [ ] apply_patch 失败时整个事务回滚，不留下半完成修改
- [ ] `symbol_search` 能找到函数、类、方法
- [ ] `find_definition` 能定位符号定义
- [ ] `find_references` 能列出符号引用
- [ ] 索引支持增量更新
- [ ] 所有新工具都有单元测试
- [ ] 新增至少 2 个端到端测试：跨文件重构、语义搜索后修改
