# coding-agent 多 Agent 与 /goals 目标管理设计

> **状态：** 设计阶段，待实现  
> **关联文档：** [主设计文档](2026-06-15-coding-agent-design.md)、[安全策略](2026-06-15-coding-agent-safety.md)、[LLM 协议](2026-06-15-coding-agent-llm-protocol.md)

## 1. 背景与目标

### 1.1 背景

当前 coding-agent 采用单 REPL + 单 LLM 客户端的架构，所有用户输入都在一个 agent loop 内完成。对于复杂任务（跨文件重构、规划-执行分离、代码审查等），单一 agent 难以同时兼顾：

- **规划**需要全局视角和只读工具
- **执行**需要写文件、跑测试
- **审查**需要独立视角找 bug
- **Git 操作**需要专用上下文

业界主流 coding agent（Roo Code、OpenCode、Aider、Claude Code、Mastra）普遍采用 Mode/Persona 或多 Agent 架构解决这一问题。

### 1.2 目标

实现**进程级并行**的多 Agent 系统：

1. **Supervisor-Worker 架构**：Supervisor 负责任务分解与调度，Worker 作为独立进程执行具体目标。
2. **`/goals` 目标管理**：持久化目标队列，支持状态跟踪、依赖、委派、恢复。
3. **角色定义**：每个角色有独立的 system prompt、工具权限和可选的模型覆盖。
4. **向后兼容**：默认保留单 agent 模式，复杂输入才触发多 agent。

### 1.3 范围边界

**包含：**

- Supervisor 与 Worker 进程间通信
- Goal 的 CRUD、持久化、DAG 依赖、状态机
- 6 个内置角色：default、architect、coder、reviewer、tester、git
- `/goals`、`/agent` REPL 命令
- Worker 工具权限隔离

**不包含（后续版本）：**

- 跨机器分布式 Worker
- Web UI 可视化 goals
- Worker 热升级
- 自动代码生成 agent（如 Copilot 式补全）

## 2. 术语表

| 术语 | 说明 |
|---|---|
| Supervisor | 任务调度器，与 REPL 同进程 |
| Worker | 独立子进程，执行单个 Goal |
| Goal | 可持久化的任务单元 |
| Role | Agent 角色，定义 system prompt 和工具权限 |
| IPC | 进程间通信 |
| UDS | Unix Domain Socket |
| HITL | Human-in-the-loop，人在回路确认 |
| Boomerang | Worker 完成任务后返回给 Supervisor，或创建子 Goal |

## 3. 总体架构

```
┌─────────────────────────────────────────┐
│           REPL / CLI (main.py)          │
│  - 用户输入解析                           │
│  - /goals 命令处理                        │
│  - 渲染结果 / 等待人工确认                 │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│           Supervisor (Orchestrator)     │
│  - 目标分解                               │
│  - Worker 生命周期管理                    │
│  - 任务分派与结果聚合                     │
│  - 状态机管理                             │
│  - 异常/超时/重试                         │
└─────────────────┬───────────────────────┘
                  │
    ┌─────────────┼─────────────┐
    │             │             │
┌───▼───┐   ┌────▼────┐  ┌─────▼─────┐
│Worker │   │ Worker  │  │  Worker   │
│Coder  │   │Reviewer │  │  Tester   │
│       │   │         │  │           │
│独立进程│   │ 独立进程 │  │  独立进程  │
└───┬───┘   └────┬────┘  └─────┬─────┘
    │            │             │
    └────────────┼─────────────┘
                 │
      ┌──────────▼──────────┐
      │   Shared State      │
      │  SQLite / JSON file │
      │  + Unix Domain Sock │
      └─────────────────────┘
```

## 4. 进程模型

### 4.1 Supervisor

- **位置**：与 REPL 同进程
- **职责**：
  - 解析用户意图，拆分为 `/goals`
  - 根据角色选择 Worker 类型
  - 启动 / 停止 Worker 进程
  - 收集 worker 结果，决定下一步
  - 处理阻塞（等待用户确认 / 需要输入）
  - 异常恢复、超时取消、重试

### 4.2 Worker

- **生命周期**：
  - **按需启动**：收到 goal 时 fork/spawn，完成后退出
  - **长生命周期池**：预先启动，减少冷启动开销（Phase 2）
- **内部结构**：
  - 独立 Python 进程
  - 加载自己的 `LLMConfig`、`system_prompt`
  - 有自己的工具 allowlist
  - 通过 IPC 向 Supervisor 报告：状态、结果、需要确认、异常
- **退出条件**：
  - goal 完成
  - 超时
  - 致命错误
  - Supervisor 显式终止

### 4.3 进程间通信（IPC）

**方案**：Unix Domain Socket + JSON 消息

| 方案 | 优点 | 缺点 |
|---|---|---|
| Unix Domain Socket | 低延迟、安全、支持全双工 | Windows 需用 named pipe 兼容 |

Windows 兼容策略：使用 `AF_UNIX` 在 Windows 10 1803+ 可用；更早版本 fallback 到 TCP localhost loopback。

## 5. 数据模型

### 5.1 Goal

```python
class GoalStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

class Goal(BaseModel):
    id: str
    parent_id: str | None
    depends_on: list[str]
    title: str
    description: str
    agent_role: str
    status: GoalStatus
    priority: int = 0
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result_summary: str | None
    error_log: list[str]
    artifacts: list[str]
```

### 5.2 Agent Role

```python
class AgentRole(BaseModel):
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] | None = None
    forbidden_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    max_steps_per_turn: int | None = None
    temperature: float | None = None
```

### 5.3 IPC Message

```python
class MessageType(str, Enum):
    ASSIGN_GOAL = "assign_goal"
    STATUS_UPDATE = "status_update"
    TOOL_REQUEST = "tool_request"
    TOOL_RESULT = "tool_result"
    NEED_CONFIRM = "need_confirm"
    USER_INPUT = "user_input"
    COMPLETE = "complete"
    ERROR = "error"
    HEARTBEAT = "heartbeat"

class IPCMessage(BaseModel):
    msg_id: str
    goal_id: str | None
    type: MessageType
    payload: dict[str, Any]
    timestamp: datetime
```

## 6. 模块设计

### 6.1 `agent/supervisor/`

| 文件 | 职责 |
|---|---|
| `supervisor.py` | Supervisor 主类，生命周期、调度、IPC server |
| `scheduler.py` | Goal DAG 解析、并发调度、worker 池 |
| `persistence.py` | SQLite 读写 Goal |
| `ipc_server.py` | Unix Domain Socket 监听 |
| `worker_pool.py` | worker 进程管理 |
| `role_loader.py` | 加载 `agents/*.yaml` |

### 6.2 `agent/worker/`

| 文件 | 职责 |
|---|---|
| `worker_main.py` | worker 进程入口 |
| `worker.py` | worker 主循环，接收 goal，调用 LLM + tools |
| `ipc_client.py` | 连接 Supervisor 的 UDS client |

### 6.3 `agents/`

```
agents/
├── default.yaml
├── architect.yaml
├── coder.yaml
├── reviewer.yaml
├── tester.yaml
└── git.yaml
```

示例 `agents/coder.yaml`：

```yaml
name: coder
description: 实现代码、写测试、运行 shell
system_prompt: |
  你是一个专注实现的开发 agent...
allowed_tools:
  - read_file
  - write_file
  - str_replace_file
  - execute_shell
  - run_tests
forbidden_tools:
  - git_commit
model: kimi-for-coding
max_steps_per_turn: 100
```

### 6.4 `agent/repl.py`

新增命令：

| 命令 | 说明 |
|---|---|
| `/goals` | 活跃目标 |
| `/goals all` | 全部目标 |
| `/goals add "<title>" [role]` | 手动添加 |
| `/goals show <id>` | 详情 |
| `/goals cancel <id>` | 取消 |
| `/goals resume <id>` | 恢复 |
| `/goals clear-done` | 清理已完成 |
| `/agent list` | 列出角色 |
| `/agent <role>` | 切换到某个角色（单 agent 模式） |

## 7. 执行流程

### 7.1 用户输入判断

```python
def _should_use_supervisor(user_input: str) -> bool:
    # 以下情况触发 Supervisor：
    # 1. 用户使用了 /goals 或 /agent 命令
    # 2. 输入长度超过阈值（如 500 字符）
    # 3. 输入包含“规划”、“重构”、“多文件”等关键词
    # 4. 配置中显式启用 multi_agent_always
    ...
```

### 7.2 Supervisor 调度流程

```
收到任务
  │
  ▼
解析意图，创建 root goal
  │
  ▼
是否需要分解？
  ├── 否 → 直接分配一个 worker
  │
  └── 是 → 拆分为子 goals
            │
            ▼
        按依赖排序
            │
            ▼
        启动可用 worker
            │
            ▼
        循环：
          1. 接收 worker 消息
          2. 更新 goal 状态
          3. 处理工具请求/确认请求
          4. 检查是否完成/失败
```

### 7.3 Worker 执行流程

```
启动
  │
  ▼
加载角色配置 + 继承 LLMConfig
  │
  ▼
连接 Supervisor UDS
  │
  ▼
等待 ASSIGN_GOAL
  │
  ▼
进入 agent loop（类似当前 REPL turn）
  │
  ├── 需要工具 → 发送 TOOL_REQUEST 给 Supervisor
  ├── 需要用户确认 → 发送 NEED_CONFIRM
  ├── 完成 → 发送 COMPLETE
  └── 异常 → 发送 ERROR
```

## 8. 工具权限

Worker 启动时根据角色配置构建 `ToolRegistry`：

```python
def build_tool_registry(role: AgentRole) -> ToolRegistry:
    registry = default_registry()
    if role.allowed_tools:
        registry = registry.subset(role.allowed_tools)
    for tool in role.forbidden_tools:
        registry.remove(tool)
    return registry
```

## 9. 持久化策略

### 9.1 位置优先级

1. `CODING_AGENT_GOALS_DB` 环境变量
2. `<workspace>/.coding-agent/goals.db`（默认）
3. `~/.coding-agent/goals.db`（fallback）

### 9.2 Schema

```sql
CREATE TABLE goals (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    depends_on TEXT, -- JSON list
    title TEXT NOT NULL,
    description TEXT,
    agent_role TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    created_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    result_summary TEXT,
    error_log TEXT, -- JSON list
    artifacts TEXT  -- JSON list
);
```

## 10. 安全

- Worker 继承 `SecurityConfig`
- 危险 shell 仍受 YOLO 模式约束
- Supervisor 负责协调，不直接执行工具
- Worker 进程 `cwd` 限制在 workspace
- Unix Domain Socket 文件权限 `0600`
- 数据库文件权限 `0600`

## 11. 测试策略

| 层级 | 内容 |
|---|---|
| 单元测试 | Goal/AgentRole 模型序列化、role_loader、scheduler DAG |
| 集成测试 | Supervisor + mock worker，验证消息协议 |
| E2E | 启动真实 worker，执行简单 multi-goal 任务 |
| 并发测试 | 多 worker 同时执行，检查文件冲突 |

## 12. 实现阶段

### Phase 1：核心骨架

- [ ] 创建 `agent/supervisor/`、`agent/worker/`、`agents/`
- [ ] 定义 `Goal`、`AgentRole`、`IPCMessage` 模型
- [ ] 实现 SQLite persistence
- [ ] 实现 UDS IPC server/client
- [ ] 实现单 worker 子进程启动与通信
- [ ] `/goals` 命令 CRUD
- [ ] 单 agent 模式兼容

### Phase 2：调度与角色

- [ ] 实现 scheduler DAG + 并发
- [ ] 加载 `agents/*.yaml`
- [ ] 工具权限隔离
- [ ] `/agent` 命令切换角色
- [ ] 自动判断何时启用 Supervisor

### Phase 3：高级能力

- [ ] Worker 阻塞点（HITL）
- [ ] Worker 崩溃恢复与重试
- [ ] Boomerang 委派（worker 创建子 goal）
- [ ] 心跳与超时
- [ ] Goal 可视化

## 13. 验收标准

- [ ] Supervisor 能启动一个 Worker 并分配 Goal
- [ ] Worker 能完成 Goal 并通过 IPC 返回结果
- [ ] `/goals` 能列出、添加、取消、恢复 Goal
- [ ] Goal 状态跨 REPL 会话持久化
- [ ] 不同角色拥有不同工具权限
- [ ] 默认单 agent 模式不受影响
- [ ] 并发执行多个无依赖 Goal 不冲突
- [ ] 所有新模块都有单元测试

## 14. 业界对标

| 需求 | 业界实现 | 我们方案 |
|---|---|---|
| 角色定义 | Roo Code Modes, OpenCode Persona | `agents/<role>.yaml` |
| 规划-执行分离 | Aider Architect mode | `architect` → `coder` |
| 子任务委派 | Roo Code Boomerang | Supervisor 派发 worker |
| 多 agent 团队 | Claude Code Agent Teams | Supervisor + Workers |
| 状态持久化 | DSRPTV SQLite checkpoint | SQLite goals.db |
| Mode 切换 | Mastra HarnessMode | `/goals` + role 调度 |

## 15. 风险与缓解

| 风险 | 缓解 |
|---|---|
| UDS 跨平台问题 | Windows fallback 到 TCP localhost |
| Worker 启动开销 | Phase 2 实现 worker 池 |
| 文件写冲突 | 调度器默认串行 coder，并行只读角色 |
| 上下文传递复杂 | Phase 1 只传 goal 文本 + 必要文件摘要 |
| 调试困难 | 每个 worker 独立日志文件 |
