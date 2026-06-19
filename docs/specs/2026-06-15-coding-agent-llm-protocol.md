# coding-agent LLM 协议规范

> **版本：** 0.2.0  
> **最后更新：** 2026-06-16

## 1. 消息格式

所有消息使用 `pydantic` 模型：

```python
class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict
```

## 2. System Prompt

System prompt 包含：

- Agent 身份和任务说明
- 当前工作目录
- 可用工具列表及其 schema
- 安全策略摘要
- 输出格式要求

```python
SYSTEM_PROMPT_TEMPLATE = """
你是一个命令行 AI 编程助手。工作目录：{workspace}

可用工具：
{tools_schema}

规则：
1. 优先使用工具完成任务
2. 危险操作会询问用户确认（YOLO 模式下不询问）
3. 所有路径必须是相对于工作目录的相对路径
4. 如果信息不足，使用 ask_user 工具询问用户
"""
```

## 3. Tool Schema 注入

从 `agent.tools.TOOL_REGISTRY` 自动生成 OpenAI tool schema：

```python
def build_tool_schema(tool: BaseTool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema.model_json_schema()
        }
    }
```

## 4. Tool Call 解析流程

1. 调用 LLM，传入 messages + tools
2. 如果 response 包含 `tool_calls`，逐个解析
3. 串行执行每个 tool call
4. 将 tool result 包装为 `tool` 消息返回给 LLM
5. LLM 再次响应，循环直到没有 tool_calls 或达到最大 step 限制

```python
for step in range(max_steps_per_turn):
    response = llm.chat(messages, tools=tools)
    if not response.tool_calls:
        break
    for call in response.tool_calls:
        result = execute_tool(call)
        messages.append(tool_message(call.id, result))
```

## 5. 错误处理

| 场景 | 处理方式 |
|---|---|
| 工具参数解析失败 | 返回错误信息，LLM 可重试 |
| 工具执行失败 | 返回异常信息，LLM 决定是否继续 |
| LLM 调用失败 | 重试 `max_retries_per_step` 次，失败后向用户报错 |
| 工具不存在 | 返回错误，LLM 修正 |
| LLM 不调用工具直接回答 | 直接输出给用户 |
| LLM 返回空 assistant 消息 | 兜底为 `"（无内容）"`，避免 400 错误 |
| LLM 调用超时 | 按 `timeout` / `stream_read_timeout` 处理 |

## 6. 流式响应

- 默认启用流式响应（`llm.stream = true`）
- 流式读取受 `stream_read_timeout` 保护
- 非流式 fallback 受 `timeout` 保护

## 7. 超时与重试

- `timeout`：单次请求总超时，默认 300 秒
- `stream_read_timeout`：流式读取单 chunk 超时，默认 120 秒
- `max_retries_per_step`：单步失败最多重试次数，默认 3
- 超过限制时向用户报告并停止

## 8. 最大轮次控制

- `max_steps_per_turn`：单次 turn 内最多 tool call 次数，默认 100
- 超过限制时向用户报告并停止

## 9. 测试用例

### Schema 生成

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 生成 read_file schema | `ReadFileTool` | schema 包含 name、description、path 参数 |
| 所有工具 schema 有效 | 16+ 个工具 | 均符合 OpenAI tool schema 格式 |
| schema 包含描述 | 任意工具 | 每个参数都有 description |

### Tool Call 解析

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 正常解析 | LLM 返回 read_file 调用 | 解析为 ToolCall(id, name, arguments) |
| 参数缺失 | LLM 返回缺少 path 的 read_file | pydantic 校验失败，返回错误 |
| 工具不存在 | LLM 返回未知工具 | 返回 "tool not found" 错误 |
| 多 tool calls | LLM 返回 3 个 tool call | 按顺序串行执行 |

### 消息流转

| 用例 | 流程 | 预期结果 |
|---|---|---|
| 单次 tool call | user -> assistant(tool_call) -> tool result -> assistant reply | 完整闭环 |
| 多次 tool call | user -> tool_call1 -> result1 -> tool_call2 -> result2 -> reply | 顺序执行 |
| 无 tool call | user -> assistant reply | 直接输出 |
| 达到 step 上限 | 循环 100 次仍有 tool_call | 停止并报告用户 |
| 空 assistant 消息 | LLM 返回空 content | 兜底为 `"（无内容）"` |

### LLM 错误

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 网络超时 | LLM API 超时 | 重试 3 次后失败 |
| 无效响应 | LLM 返回非法 JSON | 返回错误，不崩溃 |
| 空响应 | LLM 返回空内容 | 返回友好提示 |
| 流式超时 | 流式读取阻塞 | 按 `stream_read_timeout` 中断 |

### 安全相关的 tool call

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 危险工具调用 | LLM 调用 execute_shell(rm a.py) | YOLO 模式直接执行，安全模式需确认 |
| 越界路径 | LLM 调用 read_file("../x") | safety 拦截，不执行 |
