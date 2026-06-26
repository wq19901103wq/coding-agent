# SWE-bench 优化日志：coding-agent + DeepSeek v4-pro

## 背景

在 coding-agent 项目上跑 SWE-bench lite test（300 个 task 的子集），
目标是对比三种 agent 架构在同一模型（DeepSeek v4-pro）下的表现：
1. coding-agent supervisor 模式
2. coding-agent docker-bash 模式（≈ mini-swe-agent）
3. Claude Code（手动修复，作为上界参考）

## 所有实验结果

| 版本 | 模式 | 关键改动 | 分辨率 | 成功率 | 平均耗时 | pyproject.toml 污染 |
|------|------|----------|:---:|:---:|------|:---:|
| V1 | supervisor | 原始代码 | 2/10 | 5/10 | 240s | 4/10 |
| V2 | supervisor | + git gitee 镜像 + depth 500 | 2/10 | 9/10 | 344s | 4/10 |
| V3 | supervisor | + 新 coder prompt + 精简工具(16→7) | 1/5 | 5/5 | 246s | 3/5 |
| V4 | supervisor | + config stripping（bug：破坏 patch） | 0/5 | 1/5 | 263s | 0/5 |
| V5 | supervisor | 回退 stripping（=V3） | 1/5 | 5/5 | 262s | 3/5 |
| V6 | supervisor | + 强化 prompt | 1/5 | 5/5 | — | 3/5 |
| V7 | supervisor | + 对齐 Claude Code prompt | 1/5 | 5/5 | 246s | 4/5 |
| — | docker-bash + v4-flash | mini-swe-agent 风格 | 0/5 | 4/5 | 288s | 0/5 |
| — | docker-bash + v4-pro | mini-swe-agent + 推理模型 | 0/5 | 4/5 | 254s | 0/5 |
| direct V1 | direct | 零 IPC（bug：import 错误） | 0/5 | 0/5 | 96s | — |
| direct V2 | direct | 修复 import | 1/5 | 5/5 | 203s | 4/5 |
| direct V3 | direct | + execute_shell force（修复安全层拦截） | 1/5 | 5/5 | 274s | 3/5 |
| direct V4 | direct | + temperature=0 | 1/5 | 5/5 | 248s | 4/5 |
| **Claude Code** | **手动** | **v4-pro 驱动的 Claude Code** | **5/5** | **5/5** | **~30s** | **0/5** |

> 注：V3 之后只用前 5 个 astropy task 验证（节省时间）。2/10 等价于 1/5。

## 每个修复的详细分析

### 1. Git 镜像 + depth 500（V1→V2）

**问题**：shallow clone（`--depth 1`）不包含旧版 base_commit，
且 GitHub 被墙无法 `git fetch`。4 个 django task 在 30s 内崩溃。

**修复**：
- `_ensure_repo_cache`：先用 `--depth 500` 克隆，GitHub 不通时回退 gitee 镜像
- `_fetch_commit`：先本地 `git cat-file -t` 检查，存在就跳过 fetch
- 所有 git 网络操作 timeout 从 300-600s → 15s

**效果**：django 4/4 crash 修好。成功率 50% → 90%。

### 2. 精简工具 16→7（V2→V3）

**问题**：16 个工具太多，v4-pro 经常选错（如用 execute_shell+cat 代替 read_file，
用 execute_shell+sed 代替 str_replace_file）。`symbol_search`、`find_definition`、
`find_references` 三个语义重叠的工具让模型困惑。

**修复**：coder.yaml 只保留 7 个核心工具：
read_file, read_multiple_files, str_replace_file, execute_shell,
list_directory, glob_search, code_search, set_todo

**效果**：不再观察到用 execute_shell+sed 改文件的行为。

### 3. Coder prompt 迭代（V2→V3→V6→V7）

**问题**：原始 coder.yaml 只有 3 行 system_prompt。
模型没有行为约束，频繁修改 pyproject.toml、安装依赖、调试环境。

**修复历程**：
- V3：添加工作流程（5 阶段）、工具使用指南、6 条禁止规则
- V6：强化 prompt，添加工具速查表
- V7：对齐 Claude Code 的 `prompts.ts`（914 行→80 行提取精华）

**效果**：核心修复正确率从 ~40% → 100%。但 pyproject.toml 污染仍然 3-4/5，
"严禁修改配置文件"的规则拦不住 v4-pro。

**关键发现**：prompt 能提升 patch 质量（修对 vs 修错），但不能提升评测通过率。
因为：
- pyproject.toml 污染不影响评测（Docker 评测环境自带构建依赖）
- 有些正确修复不过评测（如 `operand.mask is None` vs `operand is None or operand.mask is None`）

### 4. Config stripping 尝试（V4）— 失败

**问题**：模型死都要改 pyproject.toml，想通过后处理自动 strip 掉。

**实现**：`_strip_config_changes()` 解析 unified diff，删除匹配
`pyproject.toml/setup.cfg/setup.py/Makefile/.github/` 等配置文件的 hunk。

**失败原因**：stripping 破坏了 diff 格式：
1. 文件间分隔空行变成尾部空格 → `corrupt patch at line N`
2. 移除 trailing blank lines 的逻辑有 bug

**教训**：后处理 unified diff 非常脆弱。用 `patch` 命令（更宽容）替代 `git apply` 可以缓解，
但最安全的做法是不 strip。

### 5. 去掉 Supervisor IPC — DirectAgent（direct V1-V4）

**问题**：supervisor/worker/IPC 架构是为多 worker 协作设计的（coder + reviewer + tester）。
SWE-bench 只用 1 个 coder，IPC 纯属浪费。每次工具调用：
```
Worker → IPC → Supervisor → 工具 → IPC → Worker
```
多 2 次序列化/反序列化 + 30s 超时风险。

**实现**：`DirectAgent` — 单进程 LLM 循环，工具直接调用，零 IPC。
架构等价于 Claude Code 的 agent loop。

**效果**：
- LLM 轮次：50-80 → 20-26（减半）
- 耗时：260s → 203s（1.3x 快）
- 分辨率：不变（1/5）

**一个 bug**：execute_shell 的安全分类器把所有命令标记为 "dangerous"，
DirectAgent 没有用户来确认，全部被拦截。修复：execute_shell 用 `execute_forced`。

### 6. Temperature=0（direct V4）

**想法**：v4-pro 在 temperature=0.7 下"太发散"，测试失败就跑去修环境。
降到 0.0 让它更专注。

**效果**：无变化。分辨率还是 1/5。

### 7. Docker 镜像 + Colima

**问题**：Docker Hub 被墙，`docker pull` 永久挂起。

**修复**：
- Colima 配置国内镜像（DaoCloud + 阿里云）
- `_ensure_image` pull 超时 5s → 失败后走本地构建（base + env 镜像）

**效果**：Docker 可用，docker-bash 模式能跑。

## 为什么 Claude Code 5/5，coding-agent 只能 1/5？

### 直接观察

通过 `direct V3` 的详细日志对比，同一个 task（14995）：
- **Claude Code**：Read → 看到 `operand is None` → 立刻 Edit → 1 轮完成
- **direct agent**：
  1. set_todo × 3（创建任务）
  2. code_search × 4（搜索代码）
  3. read_file × 4（读文件）
  4. ...找到 bug...
  5. execute_shell pytest → 测试失败
  6. execute_shell `setup.py build_ext --inplace` ← **被带偏了！去修环境了！**
  7. execute_shell pytest × 3 → 反复跑测试
  8. 最终超时或产出错误 patch

### 根因分析

**v4-pro 在多轮对话中有"注意力漂移"问题。** 当它看到 test failure 输出时，
容易忘记主线任务（改源码），转头去 debug 测试环境（build_ext、pip install）。

**Claude Code 能避免这个问题**，因为：
1. **上下文压缩**（microcompact/autocompact）：压缩旧消息，保持模型关注当前
2. **扩展 thinking**（`thinkingConfig`）：模型在每轮工具调用前有推理阶段，
   强制它"想清楚再动手"
3. **File state 跟踪**：Claude Code 追踪文件修改状态，Edit 之前强制 Read，
   防止模型在错误的基础上编辑
4. **更强的 prompt**：914 行系统 prompt 中有大量行为约束（"Don't add error handling,
   fallbacks, or validation for scenarios that can't happen"）

**coding-agent 缺少这些机制**：
- 无上下文压缩 → 消息越来越长，模型越来越容易分心
- 无 thinking 控制 → v4-pro 虽然有 reasoning tokens，但我们无法控制
- 无 file state 跟踪 → 模型可能重复读同一个文件
- 工具返回的 test failure 输出直接喂给模型 → 触发环境调试行为

### 尝试过的、没用的

1. API 协议（Anthropic vs OpenAI）— 用户确认不是原因
2. Temperature — 0.0 vs 0.7 无差异
3. System prompt 长度 — 3 行 vs 80 行：patch 质量改善但分辨率不变
4. 工具数量 — 16 vs 7：不再选错工具但分辨率不变

## 可用的后续方向

1. **上下文压缩**：实现滑动窗口或 summary-based compaction
2. **工具结果截断**：对 execute_shell 的 test failure 输出截断/摘要化，
   防止模型被长输出带偏
3. **thinking 控制**：研究 DeepSeek API 是否支持 reasoning token 控制
4. **换更强模型**：Claude Sonnet/Opus 在 SWE-bench 上 50%+
5. **Agent-to-agent 对比**：直接把 Claude Code（cc-connect）接入 SWE-bench runner

## 相关文件改动清单

| 文件 | 改动 |
|------|------|
| `agent/direct_agent.py` | **新建**：零 IPC 的 agent 循环 |
| `agent/agents/coder.yaml` | prompt 迭代 4 次，对齐 Claude Code |
| `agent/llm/client.py` | deepseek 模型 temperature=0 |
| `agent/tools/execute_shell.py` | 工具描述：禁止用于读/写文件 |
| `agent/tools/str_replace_file.py` | 工具描述：推荐优先使用 |
| `swe_bench/runner.py` | gitee 镜像、depth 500、direct 模式、goal description 优化 |
| `swe_bench/docker.py` | pull 超时 5s |
| `swe_bench/patch_collector.py` | config stripping（已禁用） |
| `swe_bench/cli.py` | --mode direct |
| `.env` | provider → deepseek-v4-pro |
| `config.toml` | max_steps 100→50 |
| `~/.colima/default/colima.yaml` | Docker 国内镜像 |
