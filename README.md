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
│   ├── history.py      # SQLite 持久化
│   ├── llm/            # LLM 调用层
│   ├── repl.py         # REPL 主循环
│   ├── safety.py       # 安全策略
│   └── tools/          # 工具实现
├── tests/              # 测试
├── docs/               # 设计文档和实现计划
├── main.py             # 入口
├── config.toml         # 默认配置
└── pyproject.toml      # 项目配置
```

## 设计文档

见 `docs/specs/` 和 `docs/plans/`。

## 许可证

MIT
