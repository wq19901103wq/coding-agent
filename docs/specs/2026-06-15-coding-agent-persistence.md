# coding-agent 持久化规范

> **版本：** 0.2.0  
> **最后更新：** 2026-06-16

## 1. 数据库位置

默认：`~/.coding-agent/history.db`

可通过配置 `history.db_path` 修改。

## 2. 文件权限

- SQLite 数据库文件创建时权限应设为 `0600`（仅所有者可读写）
- 目录权限应设为 `0700`
- 避免敏感会话数据被其他用户读取

## 3. 表结构

### 3.1 sessions 表

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    title TEXT
);
```

### 3.2 messages 表

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,          -- JSON
    tool_call_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

### 3.3 todos 表

```sql
CREATE TABLE todos (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

## 4. 消息序列化

- `tool_calls` 字段存储为 JSON 字符串
- `content` 为纯文本，可为空

```python
def serialize_message(msg: Message) -> dict:
    return {
        "role": msg.role,
        "content": msg.content,
        "tool_calls": json.dumps([c.model_dump() for c in msg.tool_calls]) if msg.tool_calls else None,
        "tool_call_id": msg.tool_call_id,
    }
```

## 5. 会话恢复

启动 REPL 时：

1. 如果没有指定工作目录，列出最近 5 个会话供选择
2. 如果指定了工作目录，加载该工作目录的最近会话
3. 默认恢复最近 `history.max_messages` 条消息（默认 20）

```python
def load_session(workspace: Path, limit: int = 20) -> list[Message]:
    session = get_or_create_session(workspace)
    return get_recent_messages(session.id, limit)
```

## 6. 上下文压缩

- 长会话可选择性压缩历史消息
- 压缩后保留关键决策和工具结果摘要
- 手动触发：未来通过 `/compact` 命令
- 自动触发：当消息数超过阈值时提示用户

## 7. 会话清理

- 提供 `/clear` 命令清空当前会话历史
- 不提供自动清理，避免误删

## 8. Todo 持久化

- `set_todo` 工具直接读写 `todos` 表
- 跨会话可恢复未完成的 todo
- 会话开始时展示未完成的 todo

## 9. 代码索引持久化

- 代码索引存储在独立 SQLite 数据库中（默认 `~/.coding-agent/code_index.db`）
- 索引数据库结构与历史数据库分离
- 详见 [多文件编辑与代码索引设计](2026-06-16-multi-file-and-code-index.md)

## 10. 测试用例

### 数据库初始化

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 首次启动 | 无数据库文件 | 自动创建数据库和表 |
| 已存在 | 数据库文件存在 | 不破坏已有数据 |
| 自定义路径 | `db_path="/tmp/test.db"` | 在指定路径创建 |
| 文件权限 | 新建数据库 | 权限为 `0600` |

### 会话管理

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 创建会话 | `workspace="/tmp/proj"` | 生成 session id 并入库 |
| 获取已有 | 同一路径再次启动 | 复用同一 session |
| 列出会话 | 调用 list_sessions | 返回最近 5 个会话 |
| 不同目录 | 两个不同工作目录 | 分别创建两个 session |

### 消息读写

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 保存 user 消息 | role="user", content="hi" | 成功保存 |
| 保存 assistant 消息 | role="assistant", content="ok" | 成功保存 |
| 保存 tool call | role="assistant", tool_calls=[...] | JSON 序列化后保存 |
| 保存 tool result | role="tool", tool_call_id="1", content="result" | 成功保存 |
| 恢复最近消息 | limit=20 | 按时间顺序返回最近 20 条 |
| 跨会话隔离 | session A 和 B | 各自只能读自己的消息 |

### Todo 持久化

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 创建 todo | session A 创建 2 个 todo | 保存到 todos 表 |
| 更新状态 | 更新 id=1 为 in_progress | 状态更新 |
| 完成 todo | 完成 id=1 | 状态变为 done |
| 列出 todo | session A 调用 list | 返回该 session 所有 todo |
| 跨会话恢复 | 重启后加载 session A | 未完成的 todo 仍然存在 |

### 清空会话

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 清空消息 | `/clear` | 当前 session 消息清空 |
| 清空后恢复 | 再次启动 | 消息为空 |
| 不影响其他 session | session B | 消息保留 |

### 边界情况

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 空会话 | 无消息 | 返回空列表 |
| 大量消息 | 1000 条消息 | 恢复最近 20 条，不卡顿 |
| 超长内容 | content 10000 字符 | 完整保存，不截断 |
| 非法角色 | role="xxx" | 保存失败或校验拒绝 |
