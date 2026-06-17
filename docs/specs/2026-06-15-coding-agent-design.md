# coding-agent 设计文档

> **版本：** 0.2.0  
> **最后更新：** 2026-06-16  
> **状态：** 持续演进中。P1 MVP 已完成，P2 多文件编辑/代码索引已设计，P5 多 Agent 设计中。

## 1. 项目定位

一个独立的命令行 AI 编程助手，面向**个人开发者**。用户通过 REPL 指定工作目录，agent 在该目录内读文件、写文件、执行 shell、搜索代码/网页、与用户交互，完成单文件或小范围代码任务。

与老项目（`multirole-ai`、`private-agent-project`、`superpowers-zh-kimi`）无关联，全新实现。

## 2. 术语表

| 术语 | 说明 |
|---|---|
| REPL | 读取-求值-输出循环，即命令行交互界面 |
| Turn | 一次用户输入到 agent 最终回复的完整周期 |
| Tool Call | LLM 决定调用某个工具并输出结构化参数 |
| Tool Result | 工具执行后返回给 LLM 的结果 |
| Harmless Command | 只读、不修改系统状态的 shell 命令 |
| Dangerous Command | 可能修改、删除、安装、破坏系统状态的 shell 命令 |
| YOLO 模式 | `confirm_dangerous=false`，关闭危险操作确认 |
| Supervisor | 多 agent 架构中的任务调度器 |
| Worker | 多 agent 架构中执行具体目标的独立进程 |
| Goal | 可持久化的任务单元 |

## 3. 交互模式

- 启动：`python main.py [工作目录]`（工作目录可选，默认当前目录）
- 进入 REPL，提示符：`coding-agent>`
- 用户输入自然语言指令
- Agent 自主决定调用哪些工具，完成后返回结果
- 输入 `exit` / `quit` 退出

REPL 快捷命令：`/clear`, `/model`, `/yolo`, `/help`

## 4. Scope 边界

### 4.1 当前包含

- 单文件/多文件代码读写、修改、执行
- 目录浏览和代码搜索
- 项目级代码索引与语义搜索
- 简单的网页信息查询
- 用户确认式安全控制 + YOLO 模式
- 会话历史持久化
- MCP client（实验性，可选依赖）

### 4.2 当前不包含

- 图形界面或图像处理
- 跨机器分布式 agent
- 自动安装系统级依赖
- 后台守护进程模式

## 5. 高层架构

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   用户输入   │ --> │  REPL 循环   │ --> │  LLM 客户端  │
└─────────────┘     └──────┬──────┘     └──────┬──────┘
                           │                    │
                           │     Supervisor     │
                           │   (复杂任务调度)    │
                           │                    │
                           ▼                    ▼
                  ┌─────────────────┐    ┌─────────────┐
                  │   工具分发器     │    │  Worker 进程 │
                  └────────┬────────┘    └─────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   ┌─────────┐       ┌─────────┐       ┌─────────┐
   │ 文件工具 │       │ shell工具│       │ 网络工具 │
   └─────────┘       └─────────┘       └─────────┘
```

核心模块：

- `agent.repl`：REPL 循环、快捷命令、会话管理
- `agent.llm`：LLM 调用、tool schema、tool call 解析
- `agent.tools`：16+ 个工具实现
- `agent.safety`：安全判定和用户确认
- `agent.history`：SQLite 历史持久化
- `agent.config`：配置加载和管理
- `agent.indexing`：代码索引与语义搜索
- `agent.supervisor`：多 agent 任务调度（P5）
- `agent.worker`：多 agent 工作进程（P5）

## 6. 项目结构

```
coding-agent/
├── main.py                  # REPL 入口
├── agent/
│   ├── __init__.py
│   ├── repl.py              # 交互循环
│   ├── config.py            # 配置管理
│   ├── safety.py            # 安全确认
│   ├── history.py           # SQLite 历史
│   ├── mcp_client.py        # MCP 客户端（可选依赖）
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py        # LLM 客户端
│   │   ├── schema.py        # Tool schema 生成
│   │   └── parser.py        # Tool call 解析
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py          # Tool 基类和注册
│   │   ├── read_file.py
│   │   ├── read_multiple_files.py
│   │   ├── write_file.py
│   │   ├── str_replace_file.py
│   │   ├── apply_patch.py
│   │   ├── execute_shell.py
│   │   ├── list_directory.py
│   │   ├── glob_search.py
│   │   ├── code_search.py
│   │   ├── symbol_search.py
│   │   ├── find_definition.py
│   │   ├── find_references.py
│   │   ├── web_search.py
│   │   ├── fetch_url.py
│   │   ├── ask_user.py
│   │   └── set_todo.py
│   ├── indexing/            # 代码索引
│   │   ├── __init__.py
│   │   ├── parser.py
│   │   ├── indexer.py
│   │   └── models.py
│   ├── supervisor/          # 多 agent 调度（P5）
│   │   ├── supervisor.py
│   │   ├── scheduler.py
│   │   ├── persistence.py
│   │   ├── ipc_server.py
│   │   ├── worker_pool.py
│   │   └── role_loader.py
│   └── worker/              # 多 agent 工作进程（P5）
│       ├── worker_main.py
│       ├── worker.py
│       └── ipc_client.py
├── agents/                  # 角色定义（P5）
│   ├── default.yaml
│   ├── architect.yaml
│   ├── coder.yaml
│   ├── reviewer.yaml
│   ├── tester.yaml
│   └── git.yaml
├── config.toml              # 默认配置
├── pyproject.toml
└── tests/
    ├── conftest.py
    ├── test_config.py
    ├── test_tools.py
    ├── test_safety.py
    ├── test_llm.py
    ├── test_history.py
    ├── test_repl.py
    ├── test_indexing.py
    └── e2e/
        └── ...
```

## 7. 技术栈

- Python 3.10+
- `openai` SDK（调用 Kimi / OpenAI）
- `rich`（终端输出）
- `pydantic`（配置、消息、tool schema 校验）
- `sqlite3`（历史/索引/Goal 持久化）
- `ddgs`（网页搜索）
- `requests` / `httpx`（网页抓取、LLM 超时）
- `tree-sitter` + `tree-sitter-python`（AST 索引）
- `mcp`（可选依赖，MCP client）

## 8. 端到端示例

用户输入：

```
帮我写一个计算斐波那契数列的 Python 文件，并运行它。
```

Agent 执行流程：

1. `ask_user` → 确认文件名和计算范围（可选）
2. `write_file` → 创建 `fibonacci.py`
3. `execute_shell` → 运行 `python fibonacci.py`
4. 向用户展示运行结果

## 9. 实现顺序

1. ✅ 项目脚手架和配置管理
2. ✅ LLM 调用层
3. ✅ 基础工具：read_file, write_file, list_directory, execute_shell
4. ✅ 安全策略层
5. ✅ 增强工具：str_replace_file, glob_search, code_search
6. ✅ 网络工具：web_search, fetch_url
7. ✅ 交互工具：ask_user, set_todo
8. ✅ REPL 主循环
9. ✅ 历史持久化
10. ✅ 单元测试
11. ✅ LLM 超时与空消息兜底
12. ✅ YOLO 模式与中文 readline
13. 🔄 多文件编辑与代码索引（P2）
14. 🔄 MCP client（实验性）
15. ⏳ 多 Agent 与 /goals（P5）

## 10. 验收标准

- [x] `python main.py` 能启动 REPL
- [x] 能完成一次简单的“读取文件 → 修改文件 → 执行命令”闭环
- [x] 危险操作可配置为询问用户确认或 YOLO 模式
- [x] 所有工具均有基本单元测试
- [x] 历史会话可恢复
- [x] 路径越界访问被阻止
- [x] 端到端示例可正常运行
- [x] LLM 调用支持超时配置
- [x] assistant 空消息不会导致 400 错误
- [ ] 多文件编辑与代码索引测试通过
- [ ] 多 Agent 架构跑通

## 11. 相关子 Spec

- [安全策略](2026-06-15-coding-agent-safety.md)
- [工具 Schema](2026-06-15-coding-agent-tool-schema.md)
- [LLM 协议](2026-06-15-coding-agent-llm-protocol.md)
- [配置规范](2026-06-15-coding-agent-config.md)
- [持久化规范](2026-06-15-coding-agent-persistence.md)
- [多文件编辑与代码索引](2026-06-16-multi-file-and-code-index.md)
- [多 Agent 与 /goals](2026-06-16-multi-agent.md)

## 12. 路线图

### 12.1 已完成

| 能力 | 说明 |
|---|---|
| REPL 交互 | 基本对话、快捷命令、历史恢复 |
| 工具系统 | 16+ 个文件/shell/网络/交互工具 |
| 安全策略 | 路径隔离、命令分类、YOLO 模式 |
| LLM 客户端 | OpenAI 兼容、超时、重试、User-Agent |
| 历史持久化 | SQLite sessions/messages/todos |

### 12.2 进行中

| 能力 | 说明 | 优先级 |
|---|---|---|
| 多文件编辑 | `read_multiple_files`、`apply_patch` | P1 |
| 代码索引 | tree-sitter AST + SQLite | P1 |
| MCP 支持 | MCP client，连接外部 tool server | P2 |
| 上下文压缩 | 长会话自动/手动压缩历史 | P2 |

### 12.3 规划中

| 能力 | 说明 | 优先级 |
|---|---|---|
| 多 Agent / 任务委派 | Supervisor + Worker + /goals（Phase 1 已完成） | P0 |
| `/plan` 命令 | 显式进入计划模式 | P1 |
| `/compact` 命令 | 手动压缩当前会话上下文 | P1 |
| Token / 成本估算 | 每次 turn 后显示消耗 token 数 | P2 |
| Git 状态感知 | REPL 提示符显示分支和未提交改动 | P2 |
| Batch / 脚本模式 | 支持非交互方式执行单条指令 | P2 |
| 撤销 / 重做 | 对写操作提供撤销能力 | P2 |
| 自定义 skills 系统 | 可加载外部 skill | P3 |
| Web UI 或编辑器插件 | 图形化界面 | P3 |
