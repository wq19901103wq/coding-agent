# coding-agent

独立的命令行 AI 编程助手。

[![CI](https://github.com/wq19901103wq/coding-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/wq19901103wq/coding-agent/actions/workflows/ci.yml)
[![CodeQL](https://github.com/wq19901103wq/coding-agent/actions/workflows/codeql.yml/badge.svg)](https://github.com/wq19901103wq/coding-agent/actions/workflows/codeql.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)

## 功能

- **REPL 交互**：启动后进入命令行，输入自然语言指令即可让 AI 帮你完成编程任务。
- **16 个内置工具**：读文件、写文件、局部替换、执行 shell、列目录、glob 搜索、代码搜索、网页搜索、抓取网页、询问用户、待办管理、多文件读取、批量补丁、符号搜索、定义跳转、引用查找。
- **安全策略**：写操作、危险 shell 命令需要用户确认；禁止访问工作目录外路径。
- **历史持久化**：会话消息和待办事项自动保存到 SQLite，支持跨会话恢复。
- **OpenAI 兼容后端**：默认配置为 Kimi，也可切换到其他云端或本地兼容接口。
- **多语言文件工具，Python 结构化索引**：文件读写和文本搜索适用于多种语言；符号搜索、定义跳转和引用查找目前仅解析 Python。

## 安装

```bash
# 运行环境
pip install -e .

# 仅开发者需要测试、格式化和类型检查依赖
pip install -e ".[dev]"
```

## 快速开始

```bash
# 启动 REPL，默认使用当前目录作为工作目录
python main.py

# 指定工作目录
python main.py /path/to/your/project
```

进入 REPL 后，输入自然语言指令，例如：

```
coding-agent> 写一个 hello.py，内容是 print("hello")，然后运行它
```

## REPL 快捷命令

| 命令 | 说明 |
|---|---|
| `/help` | 显示帮助 |
| `/clear` | 清屏并清空当前会话历史 |
| `/compact` | 手动压缩当前上下文 |
| `/model` | 显示当前模型 |
| `/tokens` | 显示当前上下文 token 用量 |
| `/index` | 重建代码索引 |
| `/history` | 显示历史消息摘要 |
| `/memory` | 显示已加载的项目说明和私有记忆 |
| `/memory add <内容>` | 显式加入一条项目私有记忆 |
| `/memory forget <序号>` | 删除一条项目私有记忆 |
| `/memory reload` | 重新加载项目说明与记忆 |
| `/sessions` | 列出会话 |
| `/switch` | 切换会话 |
| `/rename` | 重命名当前会话 |
| `/delete` | 删除会话 |
| `/undo` | 撤销最近一次写操作 |
| `/git` | 显示当前分支与未提交文件 |
| `/goals [list]` | 列出活跃目标 |
| `/goals "<title>" [role]` | 创建并执行一个目标 |
| `/goals show <id>` | 查看目标详情 |
| `/goals cancel <id>` | 取消目标 |
| `/goals resume <id>` | 恢复目标 |
| `/goals clear-done` | 删除已完成目标 |
| `/agent [list\|<role>]` | 列出或切换角色 |
| `/mcp` | MCP 服务器状态（实验性） |
| `/reload` | 重新加载配置与角色 |
| `/yolo on\|off\|status` | 切换危险操作确认模式；开启时需再次输入 `YOLO` |
| `exit` / `quit` | 退出 |

## 代码质量

项目通过 GitHub Actions 持续保证代码质量：

- **CI**（`.github/workflows/ci.yml`）：在 Python 3.10/3.11/3.12 上运行格式化检查（`ruff format --check`）、linter（`ruff check`）、类型检查（`mypy agent tests`）和完整测试套件（`pytest -q`）。
- **CodeQL**（`.github/workflows/codeql.yml`）：每周一及每次 `main` 分支的 push/PR 自动执行 Python 代码安全扫描。
- **Quality Tools**：
  - [Ruff](https://docs.astral.sh/ruff/) 统一负责 format 与 lint；
  - [mypy](https://mypy-lang.org/) 对 `agent` 和 `tests` 做静态类型检查；
  - [pytest](https://docs.pytest.org/) 跑单元测试与集成测试。

本地提交前建议运行：

```bash
ruff format --check
ruff check
mypy agent tests
python -m pytest -q
```

## 配置

配置文件优先级：环境变量 > `CODING_AGENT_CONFIG` 指定文件 > `~/.coding-agent/config.toml` > 项目目录 `config.toml` > 内置默认。

```toml
[llm]
provider = "kimi"
model = "kimi-for-coding"
base_url = "https://api.kimi.com/coding/v1"
api_key = ""
max_steps_per_turn = 100
max_total_tokens_per_turn = 100000
max_retries_per_step = 5

[security]
confirm_dangerous = true
log_safety_events = true

[history]
enabled = true
db_path = "~/.coding-agent/history.db"
max_messages = 20

[memory]
enabled = true
auto_save = true
max_chars = 25000
storage_root = "~/.coding-agent/projects"
```

环境变量：

- `CODING_AGENT_LLM_PROVIDER`
- `CODING_AGENT_LLM_MODEL`
- `CODING_AGENT_LLM_API_KEY`
- `CODING_AGENT_LLM_BASE_URL`
- `CODING_AGENT_LLM_MAX_TOTAL_TOKENS_PER_TURN`
- `CODING_AGENT_HISTORY_DB`
- `CODING_AGENT_MEMORY_DIR`（只能通过用户环境或显式配置重定向自动记忆目录）
- `CODING_AGENT_MEMORY_AUTO_SAVE`（设为 `0`/`false` 关闭自动写入）
- `CODING_AGENT_HISTORY_KEEP`（默认保留最近 200 个会话；设为 `0` 关闭清理）
- `CODING_AGENT_BACKUP_KEEP_DAYS`（默认 30 天；设为 `0` 关闭清理）
- `CODING_AGENT_CONFIG`

### 数据与隐私

- 默认配置连接 Kimi 云端。用户消息、模型上下文，以及模型请求读取后返回的代码或工具输出，会发送到 `base_url` 指向的服务。处理敏感仓库前，请先确认服务方的数据政策，或改用可信的 OpenAI 兼容本地端点。
- API key 应通过 `CODING_AGENT_LLM_API_KEY` 或已被 Git 忽略的 `.env` 提供，不要提交到 `config.toml`。项目不会加密配置文件。
- 历史数据库和撤销备份是本机明文文件，但会以仅当前用户可读的权限创建。可设置 `history.enabled = false` 停止保存消息；旧会话默认只保留最近 200 个，备份默认保留 30 天。
- 项目私有记忆保存在 `~/.coding-agent/projects/<仓库哈希>/memory/MEMORY.md`，不会写进仓库；同一仓库的 worktree 共享记忆，文件权限为仅当前用户可读。Agent 会自动保存已验证且未来仍有用的事实，也可通过 `/memory add` 显式写入；疑似 API key、密码和私钥会被拒绝。共享约定可放在项目根目录的 `AGENTS.md` 或 `CLAUDE.md`。仓库内配置不能重定向私有记忆目录，避免陌生项目诱导写入敏感路径。
- `safety.log` 只记录工具名、参数字段名、安全分类和成功状态，不记录参数值、命令、文件内容或工具输出。
- `fetch_url` 会拒绝 localhost、私网/链路本地 IP、含凭据 URL 和非 HTTP(S) 协议；实际抓取由 Kimi 服务执行，因此 DNS 重绑定和重定向防护仍依赖上游服务。

本地兼容端点示例：

```toml
[llm]
provider = "local"
model = "your-local-model"
base_url = "http://127.0.0.1:8000/v1"
api_key = ""
```

### 使用 `.env` 文件（推荐）

在工作目录下创建 `.env` 文件：

```bash
cp .env.example .env
# 编辑 .env，填入你的 API Key
```

启动时会自动加载工作目录下的 `.env` 文件，无需手动 export。

## 工具列表

| 工具 | 说明 |
|---|---|
| `read_file` | 读取文件内容 |
| `write_file` | 创建或覆盖文件 |
| `str_replace_file` | 局部替换文件内容 |
| `execute_shell` | 执行 harmless shell 命令 |
| `list_directory` | 列出目录内容 |
| `glob_search` | 按 glob 模式查找文件 |
| `code_search` | 代码文本搜索 |
| `read_multiple_files` | 一次读取多个文件 |
| `apply_patch` | 使用 unified diff 批量修改多个文件 |
| `symbol_search` | 按名称搜索函数、类、方法 |
| `find_definition` | 跳转到符号定义 |
| `find_references` | 查找符号引用 |
| `web_search` | 网页搜索 |
| `fetch_url` | 抓取网页内容 |
| `ask_user` | 向用户提问 |
| `set_todo` | 管理待办事项 |

## 开发

```bash
# 运行测试
python -m pytest

# 运行类型检查
mypy agent tests

# 代码格式化和检查
ruff format agent tests main.py
ruff check agent tests main.py
```

## 发布到 PyPI

```bash
# 安装构建工具
pip install build twine

# 构建
python -m build

# 上传到 PyPI（需要配置 ~/.pypirc 或环境变量 TWINE_USERNAME/TWINE_PASSWORD）
python -m twine upload dist/*
```

## SWE-bench-lite 基准测试

仓库曾在 SWE-bench-lite 的 20 个任务上做过探索性对比。该样本、执行环境和 harness 均不足以支持跨系统排名，因此目前不发布可引用的解决率；下面只保留复现方法和已知限制。

- **direct**：coding-agent 的零 IPC in-process 单 agent 模式
- **Claude Code**：通过 `cc-switch` 代理到本地端点的 Claude Code v2.1.187
- **SWE-agent**：v0.7.0，本地 persistent bash 环境

### 历史结果状态

> ⚠️ **历史数值已撤下，待使用同一公开 harness、固定环境、完整任务集并多次重复后更新。** 旧实验存在以下问题：
> 1. **数据泄露（已修）**：早期 runner 向 agent 暴露了 `FAIL_TO_PASS` 测试名，并且不同系统对 `hints_text` 的可见性不一致。当前三种模式都只接收 issue 标题和正文，隐藏测试仅供评估器使用。
> 2. **对比条件不一致**：工具集、执行环境、超时和评估器不同，结果不能作为公平的系统间比较。
> 3. **样本过小且未重复**：20 个任务的单次结果统计波动很大。
> 4. **模型标签不规范**：`deepseek-v4-flash` 是当时本地代理使用的自定义别名，不代表 DeepSeek 官方公开型号。复现时必须记录实际 provider、模型版本和端点配置。

### 关键优化

历史探索中采用过以下实现调整；这里不再把它们与已撤下的分数绑定：

1. **评测运行器显式授权**：只有受信任的 SWE-bench runner 实例能调用危险 shell 的授权入口；环境变量不能关闭普通用户的安全检查，forbidden 命令始终拒绝。
2. **Prompt 收紧**：强制最小改动、禁止安装依赖/修改配置、要求验证后再结束。
3. **合规修正**：移除 goal description 中的 `FAIL_TO_PASS` 测试名泄露，agent 只看 issue 描述，验收测试由评估 harness 在不可见情况下运行。
4. **可恢复结果**：每个系统开始和结束时都会原子保存独立状态；环境故障不会计入答错，恢复运行时优先重新验收已保存的 patch，不重复调用模型。

### 复现

```bash
# 三系统全量对比
python3 scripts/compare_three_systems.py --mode all --output-dir output/compare-three-systems-flash --model deepseek-v4-flash

# 只重跑失败任务
python3 scripts/compare_three_systems.py --mode direct --rerun-failed --output-dir output/compare-three-systems-flash --model deepseek-v4-flash
```

默认评估不会改写 SWE-bench 官方测试脚本。只有本地构建镜像存在旧版 pip/构建隔离兼容问题时，才应显式设置 `SWE_BENCH_PATCH_EVAL_ENV=1` 启用兼容模式；使用该模式得到的结果应视为本地诊断结果，不与官方榜单直接比较。

> 注：`matplotlib__matplotlib-18869` 和 `matplotlib__matplotlib-22711` 受本地 Docker env image 构建/网络限制，仍失败；`pytest-dev__pytest-11148`、`pytest-dev__pytest-5221` 为模型实现方向问题。

## 项目结构

```
coding-agent/
├── agent/              # 核心代码
│   ├── config.py       # 配置管理
│   ├── context.py      # 上下文长度管理与压缩
│   ├── history.py      # SQLite 持久化
│   ├── memory.py       # 项目说明与私有记忆
│   ├── indexing/       # 代码索引与语义搜索
│   ├── llm/            # LLM 调用层
│   ├── logging_config.py # 日志配置
│   ├── mcp_client.py   # MCP 客户端（实验性）
│   ├── repl.py         # REPL 主循环
│   ├── safety.py       # 安全策略
│   └── tools/          # 工具实现
├── tests/              # 测试
│   ├── e2e/            # 端到端测试
│   └── smoke/          # 真实 LLM 冒烟测试
├── docs/               # 设计文档和实现计划
├── main.py             # 入口
├── config.toml         # 默认配置
└── pyproject.toml      # 项目配置
```

## 技术架构

### 系统架构图

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   用户输入   │ --> │  REPL 主循环 │ --> │  LLM 客户端  │
└─────────────┘     └──────┬──────┘     └──────┬──────┘
                           │                   │
                           │     tool_calls    │
                           ▼                   ▼
                  ┌─────────────────┐   ┌─────────────┐
                  │   工具分发器     │   │  上下文管理  │
                  └────────┬────────┘   └─────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   ┌─────────┐      ┌───────────┐      ┌───────────┐
   │ 文件工具 │      │ Shell 工具 │      │ 语义搜索   │
   └─────────┘      └───────────┘      └───────────┘
```

### 核心模块说明

| 模块 | 文件 | 职责 |
|---|---|---|
| REPL 主循环 | `agent/repl.py` | 接收用户输入、调度 LLM、执行工具、维护会话状态 |
| LLM 调用层 | `agent/llm/` | 封装 OpenAI 兼容 API，支持流式/非流式、tool schema、响应解析 |
| 工具集 | `agent/tools/` | 16 个内置工具，统一继承 `BaseTool` 并自动注册 |
| 安全策略 | `agent/safety.py` | 路径越界检查、shell 命令分类、危险操作确认 |
| 历史持久化 | `agent/history.py` | SQLite 存储会话、消息、待办 |
| 上下文管理 | `agent/context.py` | token 估算、历史压缩、自动/手动 `/compact` |
| 代码索引 | `agent/indexing/` | tree-sitter 解析 Python，支持符号搜索与定义/引用查找 |
| 配置管理 | `agent/config.py` | 多源配置加载与合并 |

### 一次完整对话的数据流

1. 用户输入自然语言指令。
2. REPL 将用户输入保存为 `user` 消息，并追加到当前会话消息列表。
3. REPL 调用 `LLMClient.chat()` 或 `chat_stream()`，发送 messages + tools。
4. LLM 可能直接返回文本回复，也可能返回 `tool_calls`。
5. 如果有 `tool_calls`：
   - 对每个 tool call，REPL 调用 `_execute_tool_call()`。
   - 危险操作（写文件、危险 shell）先经用户确认。
   - 工具结果保存为 `tool` 消息返回给 LLM。
   - LLM 再次响应，循环直到没有 tool_calls 或达到最大步数。
6. 最终文本回复展示给用户；usage 累计到 `_total_usage`。
7. 所有消息持久化到 SQLite。

### 安全策略流程

```
用户输入 -> LLM 生成 tool_call
                │
                ▼
        工具参数校验
                │
                ▼
        路径越界检查  ------> 拒绝
                │
                ▼
        危险操作？
           /      \
         是        否
         │          │
         ▼          ▼
    用户确认      直接执行
    y/n/a
```

### 配置加载优先级

从高到低：

1. 环境变量（`CODING_AGENT_LLM_*`、`CODING_AGENT_HISTORY_DB` 等）
2. `CODING_AGENT_CONFIG` 指定的配置文件
3. `~/.coding-agent/config.toml`
4. workspace 目录下的 `config.toml`
5. 内置默认配置

`.env` 文件在启动时自动加载。

## 模块实现细节

### REPL 主循环（`agent/repl.py`）

`REPL` 类是整个系统的入口与调度中心：

- **初始化**：加载配置（支持 workspace 级 `config.toml`）、创建/恢复会话、构建 system prompt、连接 MCP server（如果启用）。
- **主循环 `run()`**：读取用户输入，分发 `/` 命令，调用 `_process_user_input()` 处理普通输入。
- **Turn 执行 `_run_turn()`**：核心工具调用循环：
  - 根据 `config.llm.stream` 选择 `_run_turn_stream()` 或 `_run_turn_non_stream()`。
  - 将 LLM 返回的 `AssistantResponse` 保存为 `assistant` 消息。
  - 如果存在 `tool_calls`，逐个调用 `_execute_tool_call()`，结果保存为 `tool` 消息并再次请求 LLM。
  - 只有无副作用的本地读取/搜索工具失败时自动重试 1 次；shell、写操作和网络请求不自动重试。
  - 达到 `max_steps_per_turn` 或 `max_total_tokens_per_turn` 上限后停止并提示用户。
- **历史加载 `_load_history()`**：从 SQLite 恢复最近消息，并清洗不完整的 `assistant(tool_calls)` 以及 `tool_call_id` 为空或不匹配的脏 tool 消息。
- **会话管理**：`/sessions`、`/switch`、`/rename`、`/delete` 基于 `HistoryManager` 实现；新会话自动用第一条用户消息前 30 字生成标题。
- **撤销 `/undo`**：写操作前备份原文件到 `~/.coding-agent/backups/<session_id>/<timestamp>/`，`/undo` 恢复最近一次备份；默认自动清理 30 天前的备份。
- **Git 状态**：启动时与 `/git` 命令通过 `git status --short` 和 `git branch --show-current` 展示当前分支与未提交文件。

### LLM 调用层（`agent/llm/`）

- **`client.py`**：`LLMClient` 封装 OpenAI 兼容 SDK。
  - `_build_client()`：注入 `User-Agent: KimiCLI/1.30.0`（kimi 端点），允许空 API key 创建客户端以便启动/测试，真实鉴权错误在实际调用时抛出。
  - `_prepare_messages()`：将内部 `Message` 模型转换为 OpenAI payload，过滤空 `tool_call_id`，并记录调试日志帮助排查 tool_call_id 问题。
  - `_build_kwargs()`：根据模型自动强制 `temperature=1.0`（`kimi-for-coding`）。
  - `chat()` / `chat_stream()`：指数退避重试（`2^attempt` 秒），捕获 `APIError`、`APIConnectionError`、`APITimeoutError`、`RateLimitError`。
  - `_parse_stream()`：流式 chunk 聚合，为缺失 id 的 tool call 生成稳定的 `call_<uuid>` fallback id。
- **`parser.py`**：非流式响应解析，`parse_assistant_response()` 提取 content、tool_calls 和 usage；`_parse_tool_call()` 同样为缺失 id 的调用生成 fallback。
- **`schema.py`**：定义 `Message`、`ToolCall`、`AssistantResponse`、`Usage`、`LLMError` 等核心数据模型。
- **`tools.py`**：`build_tool_schema()` / `build_tools_payload()` 将 `BaseTool` 转换为 OpenAI function schema。

### 工具系统（`agent/tools/`）

- **`BaseTool`**（`base.py`）：所有工具的抽象基类，要求定义 `name`、`description`、`input_schema`（Pydantic BaseModel）和 `execute(input, ctx)`。
- **自动注册**（`__init__.py`）：模块导入时实例化全部 16 个内置工具并写入 `TOOL_REGISTRY`，`get_tool(name)` 按名称分发。
- **内置工具**：
  - 文件：`read_file`、`read_multiple_files`、`write_file`、`str_replace_file`、`apply_patch`、`list_directory`、`glob_search`
  - 代码索引：`symbol_search`、`find_definition`、`find_references`、`code_search`
  - 执行：`execute_shell`
  - 网络：`web_search`、`fetch_url`
  - 交互/任务：`ask_user`、`set_todo`
- **`ApplyPatchTool`**：解析 unified diff，校验路径、原子备份、应用 hunks，失败时回滚。
- **MCP 适配（实验性）**：`mcp_client.py` 用 `asyncio.run` 包装 stdio MCP client；`agent/tools/mcp_adapter.py` 将 MCP 工具桥接到 `BaseTool`。未安装 `mcp` 包时模块仍可导入，实例化时抛出清晰错误。

### 安全策略（`agent/safety.py`）

- **路径校验 `validate_path()`**：将相对路径解析为绝对路径后，用 `Path.relative_to()` 确保目标位于 workspace 内，防止 `../` 等越界访问。
- **Shell 命令分类 `classify_shell_command()`**：
  1. 先匹配 `FORBIDDEN_PATTERNS`（`sudo`、`su`、`rm -rf /`、`dd`、`mkfs`、`/etc/passwd`、`~/.ssh` 等）→ 直接拒绝。
  2. 再识别无害命令：`git status/log/diff/show`、`python -c`（代码无危险模式）、白名单命令（`ls`、`cat`、`grep`、`find` 等）以及仅由白名单命令组成的管道。
  3. 命中 `DANGEROUS_PATTERNS`（`rm`、`cp`、`mv`、`pip install`、`curl`、`ssh`、重定向、管道符、分号等）→ 标记为危险，需用户确认。
- **确认交互**：危险命令和写文件工具在 `REPL._execute_tool_call()` 中调用 `_confirm_dangerous()`，每次都需要用户输入 `y/n`，`execute_shell` 不提供永久放行选项。

### 历史持久化（`agent/history.py`）

`HistoryManager` 基于 SQLite 管理三类数据：

- **sessions**：会话 ID、workspace、标题、创建/更新时间。
- **messages**：按 `session_id` 外键存储 role、content、tool_calls（JSON）、tool_call_id。
- **todos**：待办事项 ID、标题、状态（pending/in_progress/done）。

关键方法：`create_session`、`get_or_create_session`、`list_recent_sessions`、`load_messages`、`save_message`、`rename_session`、`delete_session`、`update_session_title`。数据库路径默认为 `~/.coding-agent/history.db`。

### 项目记忆（`agent/memory.py`）

项目记忆与聊天历史分开：历史记录保存一次会话说过什么，项目记忆保存跨会话仍有用的项目事实和约定。

- 自动读取全局 `~/.coding-agent/AGENTS.md`，以及项目根目录的 `AGENTS.md`、`CLAUDE.md`。
- Agent 在工作中通过受保护的 `remember_project_memory` 工具自动保存稳定事实，无需用户逐条确认；可设置 `memory.auto_save = false` 关闭。
- `/memory add <内容>` 仍可显式写入；`forget`、`clear` 和 `reload` 用于审计和管理。
- 普通 REPL 与 supervisor worker 都会获得相同记忆；SWE-bench 的 Direct/Claude/SWE-agent 对比路径不加载该记忆，避免改变基准提示词。
- 启动时最多注入 `MEMORY.md` 前 200 行且受 `memory.max_chars` 限制，并明确不能覆盖安全规则。当前版本不额外启动记忆模型或做向量检索，因此没有额外模型调用成本。

### 上下文管理（`agent/context.py`）

`ContextManager` 负责控制 LLM 上下文长度：

- **Token 估算 `estimate_tokens()`**：字符近似法，每条消息固定 50 token 开销 + content 长度除以 4（保守估计中文/英文混合场景），每个 tool_call 额外 100 token。
- **阈值判断 `is_near_limit()`**：`estimate_tokens() >= config.max_tokens`。
- **压缩 `compact()`**：保留 system prompt 和最近 `preserve_recent` 条消息，中间部分通过 LLM 生成中文摘要（300 字以内），替换为一条 `[上下文摘要]` system 消息。
- **自动压缩**：REPL 在每次 turn 无 tool_calls 返回时调用 `_maybe_auto_compact()`。

### 代码索引（`agent/indexing/`）

基于 tree-sitter 解析 Python 代码并建立本地 SQLite 索引：

- **`parser.py`**：遍历 workspace 下所有 `.py` 文件，用 tree-sitter 提取函数、类、变量定义及引用。
- **`indexer.py`**：`Indexer.build()` 将符号和引用写入 `~/.coding-agent/code_index.db`，包含 `symbols`、`symbol_references`、`files`（mtime）三张表。
- **工具集成**：`symbol_search` 按名称模糊搜索符号；`find_definition` / `find_references` 查询定义与引用位置；`code_search` 支持按内容或类型过滤。
## 设计文档

见 `docs/specs/` 和 `docs/plans/`。

## 许可证

MIT
