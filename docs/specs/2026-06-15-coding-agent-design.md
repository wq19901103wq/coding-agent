# coding-agent 设计文档

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

## 3. 交互模式

- 启动：`python main.py [工作目录]`（工作目录可选，默认当前目录）
- 进入 REPL，提示符：`coding-agent>`
- 用户输入自然语言指令
- Agent 自主决定调用哪些工具，完成后返回结果
- 输入 `exit` / `quit` 退出

REPL 快捷命令：`/clear`, `/model`, `/help`

## 4. 首期 Scope 边界

**包含：**
- 单文件代码读写、修改、执行（最多 3-5 个文件）
- 目录浏览和代码搜索
- 简单的网页信息查询
- 用户确认式安全控制
- 会话历史持久化

**不包含（后续版本）：**
- 多文件大型重构
- 图形界面或图像处理
- 长时间后台进程
- 跨工作目录操作
- 自动安装系统级依赖

## 5. 高层架构

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   用户输入   │ --> │  REPL 循环   │ --> │  LLM 客户端  │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                        ┌──────────────────────┘
                        │ tool_calls
                        ▼
               ┌─────────────────┐
               │   工具分发器     │
               └────────┬────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   ┌─────────┐    ┌─────────┐    ┌─────────┐
   │ 文件工具 │    │ shell工具│    │ 网络工具 │
   └─────────┘    └─────────┘    └─────────┘
```

核心模块：
- `agent.repl`：REPL 循环、快捷命令、会话管理
- `agent.llm`：LLM 调用、tool schema、tool call 解析
- `agent.tools`：11 个工具实现
- `agent.safety`：安全判定和用户确认
- `agent.history`：SQLite 历史持久化
- `agent.config`：配置加载和管理

## 6. 项目结构

```
coding-agent/
├── main.py                  # REPL 入口
├── agent/
│   ├── __init__.py
│   ├── repl.py              # 交互循环
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py        # LLM 客户端
│   │   ├── schema.py        # Tool schema 生成
│   │   └── parser.py        # Tool call 解析
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py          # Tool 基类和注册
│   │   ├── read_file.py
│   │   ├── write_file.py
│   │   ├── str_replace_file.py
│   │   ├── execute_shell.py
│   │   ├── list_directory.py
│   │   ├── glob_search.py
│   │   ├── code_search.py
│   │   ├── web_search.py
│   │   ├── fetch_url.py
│   │   ├── ask_user.py
│   │   └── set_todo.py
│   ├── safety.py            # 安全确认
│   ├── history.py           # SQLite 历史
│   └── config.py            # 配置管理
├── config.toml              # 默认配置
└── tests/
    ├── conftest.py
    ├── test_tools.py
    ├── test_safety.py
    ├── test_llm.py
    ├── test_config.py
    └── test_history.py
```

## 7. 技术栈

- Python 3.10+
- `openai` SDK（调用 Kimi / OpenAI）
- `rich`（终端输出）
- `pydantic`（配置、消息、tool schema 校验）
- `sqlite3`（历史持久化）
- `ddgs`（网页搜索）
- `requests` / `httpx`（网页抓取）

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

1. 项目脚手架和配置管理
2. LLM 调用层
3. 基础工具：read_file, write_file, list_directory, execute_shell
4. 安全策略层
5. 增强工具：str_replace_file, glob_search, code_search
6. 网络工具：web_search, fetch_url
7. 交互工具：ask_user, set_todo
8. REPL 主循环
9. 历史持久化
10. 单元测试

## 10. 验收标准

- [ ] `python main.py` 能启动 REPL
- [ ] 能完成一次简单的“读取文件 → 修改文件 → 执行命令”闭环
- [ ] 危险操作会询问用户确认
- [ ] 11 个工具均有基本单元测试
- [ ] 历史会话可恢复
- [ ] 路径越界访问被阻止
- [ ] 端到端示例可正常运行

## 11. 相关子 Spec

- [安全策略](2026-06-15-coding-agent-safety.md)
- [工具 Schema](2026-06-15-coding-agent-tool-schema.md)
- [LLM 协议](2026-06-15-coding-agent-llm-protocol.md)
- [配置规范](2026-06-15-coding-agent-config.md)
- [持久化规范](2026-06-15-coding-agent-persistence.md)

## 12. 二期路线图

基于与 Kimi Code CLI 和 Claude Code 的能力对比，首期 MVP 之外的规划：

### 12.1 大缺口（建议二期实现）

| 能力 | 说明 | 优先级 |
|---|---|---|
| 子 Agent / 任务委派 | 增加 `delegate_task` 工具，把独立子任务派给子 agent | P0 |
| 上下文压缩 | 实现 `compaction`，长会话自动/手动压缩历史 | P0 |
| MCP 支持 | 增加 MCP client，连接外部 tool server | P1 |
| 图像/媒体理解 | 增加 `read_image` 工具 | P1 |
| 并行工具调用 | 支持一次返回多个无依赖 tool call 并行执行 | P1 |

### 12.2 小缺口（建议二期或快速补齐）

| 能力 | 说明 | 优先级 |
|---|---|---|
| `/plan` 命令 | 显式进入计划模式，基于 `set_todo` 规划任务 | P1 |
| `/compact` 命令 | 手动压缩当前会话上下文 | P1 |
| Token / 成本估算 | 每次 turn 后显示消耗 token 数 | P2 |
| Git 状态感知 | REPL 提示符显示分支和未提交改动 | P2 |
| Batch / 脚本模式 | 支持非交互方式执行单条指令 | P2 |
| 撤销 / 重做 | 对写操作提供撤销能力 | P2 |

### 12.3 三期方向

- 多文件重构支持
- 项目级代码索引
- 自定义 skills 系统
- Web UI 或编辑器插件
