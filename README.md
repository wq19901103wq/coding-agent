# coding-agent

独立的命令行 AI 编程助手。

[![CI](https://github.com/wq19901103wq/coding-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/wq19901103wq/coding-agent/actions/workflows/ci.yml)

## 功能

- **REPL 交互**：启动后进入命令行，输入自然语言指令即可让 AI 帮你完成编程任务。
- **16 个内置工具**：读文件、写文件、局部替换、执行 shell、列目录、glob 搜索、代码搜索、网页搜索、抓取网页、询问用户、待办管理、多文件读取、批量补丁、符号搜索、定义跳转、引用查找。
- **安全策略**：写操作、危险 shell 命令需要用户确认；禁止访问工作目录外路径。
- **历史持久化**：会话消息和待办事项自动保存到 SQLite，支持跨会话恢复。
- **双模型后端**：默认 Kimi，支持 OpenAI 兼容接口切换。

## 安装

```bash
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
| `/model` | 显示当前模型 |
| `/index` | 重建代码索引 |
| `exit` / `quit` | 退出 |

## 配置

配置文件优先级：环境变量 > `CODING_AGENT_CONFIG` 指定文件 > `~/.coding-agent/config.toml` > 项目目录 `config.toml` > 内置默认。

```toml
[llm]
provider = "kimi"
model = "kimi-for-coding"
base_url = "https://api.kimi.com/coding/v1"
api_key = ""
max_steps_per_turn = 100
max_retries_per_step = 3

[security]
confirm_dangerous = true
log_safety_events = true

[history]
enabled = true
db_path = "~/.coding-agent/history.db"
max_messages = 20
```

环境变量：

- `CODING_AGENT_LLM_PROVIDER`
- `CODING_AGENT_LLM_MODEL`
- `CODING_AGENT_LLM_API_KEY`
- `CODING_AGENT_LLM_BASE_URL`
- `CODING_AGENT_HISTORY_DB`
- `CODING_AGENT_CONFIG`

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

## 项目结构

```
coding-agent/
├── agent/              # 核心代码
│   ├── config.py       # 配置管理
│   ├── context.py      # 上下文长度管理与压缩
│   ├── history.py      # SQLite 持久化
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

## 设计文档

见 `docs/specs/` 和 `docs/plans/`。

## 许可证

MIT
