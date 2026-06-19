# coding-agent 工具 Schema 规范

> **版本：** 0.2.0  
> **最后更新：** 2026-06-16

## 1. 设计原则

- 每个工具一个模块：`agent/tools/<tool_name>.py`
- 统一继承 `BaseTool`，实现 `name`, `description`, `input_schema`, `execute()`
- 工具通过 `agent/tools/__init__.py` 自动注册
- 所有路径参数必须是相对于工作目录的字符串
- 工具输出统一包装为 `ToolResult`

## 2. ToolResult 标准结构

```python
class ToolResult(BaseModel):
    success: bool
    output: str | None = None
    error: str | None = None
    metadata: dict | None = None
```

- `success=True`：工具执行成功，`output` 必填
- `success=False`：工具执行失败，`error` 必填
- `metadata`：可选，用于传递额外信息（如搜索结果条数、文件大小等）

## 3. 工具注册机制

```python
# agent/tools/base.py
class BaseTool(ABC):
    name: str
    description: str
    input_schema: Type[BaseModel]

    @abstractmethod
    def execute(self, input: dict, ctx: ToolContext) -> ToolResult: ...

# agent/tools/__init__.py
TOOL_REGISTRY: dict[str, BaseTool] = {}

def register_tool(tool: BaseTool):
    TOOL_REGISTRY[tool.name] = tool
```

## 4. 各工具 Schema

### read_file

```python
class ReadFileInput(BaseModel):
    path: str = Field(..., description="相对于工作目录的文件路径")

class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取指定文件内容"
    input_schema = ReadFileInput
```

### read_multiple_files

```python
class ReadMultipleFilesInput(BaseModel):
    paths: list[str] = Field(..., description="相对于工作目录的文件路径列表")

class ReadMultipleFilesTool(BaseTool):
    name = "read_multiple_files"
    description = "一次读取多个文件内容，适用于跨文件任务"
    input_schema = ReadMultipleFilesInput
```

- 一次读取多个文件，返回合并后的内容
- 每个文件之间用分隔线区分
- 超长自动截断（默认 `MAX_OUTPUT_LENGTH = 8000`）

### write_file

```python
class WriteFileInput(BaseModel):
    path: str
    content: str
    append: bool = False

class WriteFileTool(BaseTool):
    name = "write_file"
    description = "创建或覆盖文件"
    input_schema = WriteFileInput
```

### str_replace_file

```python
class StrReplaceFileInput(BaseModel):
    path: str
    old_str: str
    new_str: str

class StrReplaceFileTool(BaseTool):
    name = "str_replace_file"
    description = "局部替换文件内容"
    input_schema = StrReplaceFileInput
```

### apply_patch

```python
class ApplyPatchInput(BaseModel):
    diff: str = Field(..., description="unified diff 格式的补丁文本")

class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = "使用 unified diff 同时修改多个文件，支持新增和删除文件"
    input_schema = ApplyPatchInput
```

- 解析 diff，应用到多个文件
- 支持新增、删除、修改文件
- 每个文件的修改必须唯一匹配
- 如果某个 hunks 匹配失败，整个 patch 回滚，返回错误
- 属于写操作，需要确认（YOLO 模式下直接执行）

### execute_shell

```python
class ExecuteShellInput(BaseModel):
    command: str
    timeout: int = 30

class ExecuteShellTool(BaseTool):
    name = "execute_shell"
    description = "执行 shell 命令"
    input_schema = ExecuteShellInput
```

> 注意：`timeout` 默认值 30 秒，但最终受 `LLMConfig.timeout` 限制。

### list_directory

```python
class ListDirectoryInput(BaseModel):
    path: str = "."

class ListDirectoryTool(BaseTool):
    name = "list_directory"
    description = "列出目录内容"
    input_schema = ListDirectoryInput
```

### glob_search

```python
class GlobSearchInput(BaseModel):
    pattern: str

class GlobSearchTool(BaseTool):
    name = "glob_search"
    description = "按 glob 模式查找文件"
    input_schema = GlobSearchInput
```

### code_search

```python
class CodeSearchInput(BaseModel):
    pattern: str
    path: str = "."

class CodeSearchTool(BaseTool):
    name = "code_search"
    description = "代码文本搜索"
    input_schema = CodeSearchInput
```

### symbol_search

```python
class SymbolSearchInput(BaseModel):
    query: str = Field(..., description="符号名称或通配符")
    kind: str | None = Field(default=None, description="符号类型：function/class/method/variable")

class SymbolSearchTool(BaseTool):
    name = "symbol_search"
    description = "按名称/类型搜索代码符号"
    input_schema = SymbolSearchInput
```

### find_definition

```python
class FindDefinitionInput(BaseModel):
    name: str = Field(..., description="符号名称")
    path: str = Field(default=".", description="搜索起点目录")

class FindDefinitionTool(BaseTool):
    name = "find_definition"
    description = "查找符号定义位置"
    input_schema = FindDefinitionInput
```

### find_references

```python
class FindReferencesInput(BaseModel):
    name: str = Field(..., description="符号名称")
    path: str = Field(default=".", description="搜索起点目录")

class FindReferencesTool(BaseTool):
    name = "find_references"
    description = "查找符号所有引用位置"
    input_schema = FindReferencesInput
```

### web_search

```python
class WebSearchInput(BaseModel):
    query: str
    max_results: int = 5

class WebSearchTool(BaseTool):
    name = "web_search"
    description = "网页搜索"
    input_schema = WebSearchInput
```

### fetch_url

```python
class FetchURLInput(BaseModel):
    url: str
    max_length: int = 5000

class FetchURLTool(BaseTool):
    name = "fetch_url"
    description = "抓取网页内容"
    input_schema = FetchURLInput
```

### ask_user

```python
class AskUserInput(BaseModel):
    question: str
    options: list[str] | None = None

class AskUserTool(BaseTool):
    name = "ask_user"
    description = "向用户提问"
    input_schema = AskUserInput
```

### set_todo

```python
class SetTodoInput(BaseModel):
    action: Literal["create", "update", "complete", "list"]
    id: str | None = None
    title: str | None = None
    status: Literal["pending", "in_progress", "done"] | None = None

class SetTodoTool(BaseTool):
    name = "set_todo"
    description = "任务清单管理"
    input_schema = SetTodoInput
```

## 5. 通用约束

- 路径参数统一用相对路径，工具内部解析为绝对路径并校验
- 输出截断阈值：
  - `read_multiple_files`：8000 字符
  - 其他工具：5000 字符
- 截断后在 `metadata` 中标记 `truncated=True` 和 `original_length`
- 所有工具捕获异常并返回 `ToolResult(success=False, error=...)`

## 6. 工具权限（多 Agent 场景）

- 每个 Agent 角色可配置 `allowed_tools` 和 `forbidden_tools`
- `allowed_tools` 为 `None` 表示允许所有工具
- `forbidden_tools` 优先级高于 `allowed_tools`
- 详见 [多 Agent 设计](2026-06-16-multi-agent.md)

## 7. 测试用例

### read_file

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 正常读取 | `path="hello.py"` | 返回文件内容 |
| 文件不存在 | `path="not_exist.py"` | `success=False`, error 包含 "File not found" |
| 路径越界 | `path="../outside.txt"` | `success=False`, error 包含 "Path outside workspace" |
| 读取目录 | `path="."` | `success=False`, error 包含 "Is a directory" |

### read_multiple_files

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 读取多个 | `paths=["a.py", "b.py"]` | 返回合并内容 |
| 部分缺失 | `paths=["a.py", "missing.py"]` | `success=False` |
| 超长截断 | 总长度 > 8000 | `metadata.truncated=True` |

### write_file

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 创建文件 | `path="a.py", content="x=1"` | 文件创建成功，返回确认 |
| 覆盖文件 | `path="a.py", content="x=2"` | 内容被覆盖 |
| 追加内容 | `path="a.py", content="\ny=2", append=True` | 内容追加到末尾 |
| 路径越界 | `path="../x.py"` | 返回失败，不写入 |
| 写入目录 | `path="src/"` | 返回失败 |

### str_replace_file

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 正常替换 | `old_str="x=1", new_str="x=2"` | 仅替换匹配部分 |
| 无匹配 | `old_str="not_exist"` | `success=False` |
| 多处匹配 | `old_str="a"` 出现 2 次 | `success=False`，要求唯一匹配 |
| 路径越界 | `path="../x.py"` | `success=False` |

### apply_patch

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 单文件 | unified diff 1 个文件 | 成功应用 |
| 多文件 | unified diff 多个文件 | 全部应用 |
| 新增文件 | `--- /dev/null` | 创建文件 |
| 部分失败 | 某个 hunk 不匹配 | 整体回滚 |

### execute_shell

| 用例 | 输入 | 预期结果 |
|---|---|---|
| harmless 命令 | `command="pwd"` | 直接执行，返回输出 |
| 危险命令 | `command="rm a.py"` | YOLO 模式直接执行，安全模式需确认 |
| 超时 | `command="sleep 10", timeout=1` | `success=False`，超时错误 |
| 命令不存在 | `command="not_exist_cmd"` | `success=False` |
| 路径越界尝试 | `command="cat ../secret.txt"` | 由 safety 层拦截 |

### list_directory

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 列当前目录 | `path="."` | 返回文件/目录列表 |
| 列子目录 | `path="src"` | 返回 src 内容 |
| 目录不存在 | `path="not_exist"` | `success=False` |
| 路径越界 | `path="../"` | `success=False` |

### glob_search

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 查找 py 文件 | `pattern="**/*.py"` | 返回匹配文件列表 |
| 无匹配 | `pattern="**/*.not_exist"` | 返回空列表 |
| 越界模式 | `pattern="../**/*"` | `success=False` |

### code_search

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 匹配 | `pattern="def main"` | 返回匹配行和文件 |
| 无匹配 | `pattern="class NotExist"` | 返回空列表 |
| 越界路径 | `path="../"` | `success=False` |

### symbol_search / find_definition / find_references

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 搜索函数 | `query="helper"` | 返回符号列表 |
| 按类型过滤 | `query="Bar", kind="class"` | 返回类定义 |
| 查找定义 | `name="foo"` | 返回定义位置 |
| 查找引用 | `name="UserService"` | 返回所有引用位置 |

### web_search

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 正常搜索 | `query="python pydantic"` | 返回最多 5 条结果 |
| 网络失败 | 模拟无网络 | `success=False` 或空列表 + error |
| 空查询 | `query=""` | `success=False` |

### fetch_url

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 正常抓取 | `url="https://example.com"` | 返回网页文本 |
| 无效 URL | `url="not-a-url"` | `success=False` |
| 超时 | `url="https://httpbin.org/delay/10"`, timeout=1 | `success=False` |

### ask_user

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 选项式提问 | `question="选哪个？", options=["A","B"]` | REPL 显示选项并等待输入 |
| 开放式提问 | `question="文件名？"` | REPL 显示问题并等待输入 |

### set_todo

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 创建 todo | `action="create", title="实现 read_file"` | 创建成功，返回 id |
| 更新状态 | `action="update", id="1", status="in_progress"` | 状态更新 |
| 完成 todo | `action="complete", id="1"` | 状态变为 done |
| 列出 todo | `action="list"` | 返回所有 todo |
