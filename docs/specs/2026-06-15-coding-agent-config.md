# coding-agent 配置规范

## 1. 配置文件位置

按优先级查找：

1. 环境变量 `CODING_AGENT_CONFIG` 指定的路径
2. `~/.coding-agent/config.toml`
3. 项目目录下的 `config.toml`
4. 内置默认配置

## 2. 配置加载优先级

- 环境变量 > 用户配置 > 项目配置 > 内置默认
- 后加载的配置不会完全覆盖前者，而是逐键合并

## 3. 配置项

```toml
[llm]
provider = "kimi"                    # kimi | openai
model = "kimi-for-coding"
base_url = "https://api.kimi.com/coding/v1"
api_key = ""                         # 或从环境变量读取
max_steps_per_turn = 100
max_retries_per_step = 3

[llm.openai]
model = "gpt-4o"
base_url = "https://api.openai.com/v1"
api_key = ""

[security]
confirm_dangerous = true
log_safety_events = true
allow_outside_workspace = false

[history]
enabled = true
db_path = "~/.coding-agent/history.db"
max_messages = 20

[output]
theme = "default"
verbose = false
```

## 4. 环境变量映射

| 环境变量 | 对应配置 |
|---|---|
| `CODING_AGENT_LLM_PROVIDER` | `llm.provider` |
| `CODING_AGENT_LLM_MODEL` | `llm.model` |
| `CODING_AGENT_LLM_API_KEY` | `llm.api_key` |
| `CODING_AGENT_LLM_BASE_URL` | `llm.base_url` |
| `CODING_AGENT_HISTORY_DB` | `history.db_path` |

## 5. 配置校验

使用 `pydantic` 模型校验：

```python
class Config(BaseModel):
    llm: LLMConfig
    security: SecurityConfig
    history: HistoryConfig
    output: OutputConfig
```

校验规则：
- `llm.provider` 必须是 `kimi` 或 `openai`
- `max_steps_per_turn` >= 1
- `max_retries_per_step` >= 0
- `history.max_messages` >= 0

## 6. 测试用例

### 配置加载

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 无配置文件 | 只有内置默认 | 使用默认配置 |
| 用户配置覆盖 | `~/.coding-agent/config.toml` 存在 | 合并用户配置 |
| 环境变量最高 | 同时存在环境变量和用户配置 | 环境变量生效 |
| 指定路径 | `CODING_AGENT_CONFIG=/tmp/a.toml` | 加载指定文件 |
| 项目配置 | 项目目录下有 `config.toml` | 作为最低优先级合并 |

### 配置合并

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 部分覆盖 | 用户只配置了 `llm.model` | 其他项保持默认 |
| 嵌套覆盖 | 用户配置 `llm.max_steps_per_turn=200` | 仅该项覆盖 |
| 无效 provider | `provider="x"` | 校验失败，抛出 ConfigError |
| 负数 step | `max_steps_per_turn=-1` | 校验失败 |

### 环境变量

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 设置 provider | `CODING_AGENT_LLM_PROVIDER=openai` | `config.llm.provider == "openai"` |
| 设置 api_key | `CODING_AGENT_LLM_API_KEY=sk-xxx` | `config.llm.api_key == "sk-xxx"` |
| 空环境变量 | 环境变量存在但为空 | 视为未设置 |

### API Key 安全

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 配置文件中的 key | `api_key="sk-xxx"` | 加载后可用 |
| 环境变量 key | `CODING_AGENT_LLM_API_KEY=sk-xxx` | 优先使用 |
| key 缺失 | 无 key | 启动时提示配置 |
