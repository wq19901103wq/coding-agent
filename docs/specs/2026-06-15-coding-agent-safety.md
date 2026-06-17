# coding-agent 安全策略规范

> **版本：** 0.2.0  
> **最后更新：** 2026-06-16

## 1. 设计原则

- **默认拒绝**：任何未明确允许的操作都视为危险
- **路径隔离**：所有文件/目录操作限定在工作目录内
- **可配置确认**：危险操作可配置为需要用户确认，或 YOLO 模式直接执行
- **审计日志**：所有敏感操作记录到日志

## 2. 工作目录边界

- 工作目录由启动参数或运行时指定，解析为绝对路径
- 任何工具接收的相对路径，最终必须落在工作目录内
- 软链接、硬链接、相对路径 `..` 一律解析为真实绝对路径后判断

```python
def is_within_workspace(path: str, workspace: Path) -> bool:
    real_path = (workspace / path).resolve()
    return str(real_path).startswith(str(workspace.resolve()))
```

## 3. Shell 命令分类

### 3.1 Harmless（直接执行）

只允许以下命令及其常见参数：

- `ls`, `cat`, `head`, `tail`, `less`（只读模式）
- `grep`, `find`, `rg`, `awk`（不写入文件）
- `pwd`, `echo`, `which`, `python -c`（不修改状态）
- `git status`, `git log`, `git diff`（只读 git 操作）

判定规则：命令在白名单内，且不包含重定向/管道到写操作、不包含 `&&`/`|` 连接的命令。

### 3.2 Dangerous（需确认 / YOLO 模式直接执行）

- 写操作：`>`, `>>`, `cp`, `mv`, `rm`, `mkdir`, `touch`, `tee`
- 安装：`pip install`, `brew install`, `npm install`, `apt-get`
- 网络：`curl`, `wget`, `ssh`, `scp`
- 系统级：`sudo`, `kill`, `chmod`, `chown`, `systemctl`
- 执行脚本/二进制：`bash script.sh`, `./a.out`
- 包含 `&&`, `||`, `|`, `;` 等组合命令

### 3.3 Forbidden（禁止执行）

- 访问工作目录外敏感路径：`~/.ssh`, `/etc`, `/usr/bin`, `/bin`, `~/.bashrc`
- `sudo`, `su`, `doas`
- `rm -rf /`, `dd`, `mkfs`
- 修改环境变量并执行：`export PATH=...; cmd`

## 4. 路径安全校验

所有工具在执行前必须校验路径：

```python
def validate_path(path: str, workspace: Path) -> Path:
    target = (workspace / path).resolve()
    if not str(target).startswith(str(workspace.resolve())):
        raise PathOutsideWorkspaceError(path)
    return target
```

## 5. 用户确认流程

### 5.1 安全模式（`confirm_dangerous = true`）

危险操作触发时，REPL 显示：

```
⚠️  危险操作需要确认：
   工具: write_file
   路径: src/main.py
   操作: 覆盖文件（原文件 120 bytes）

是否执行？(y/n): 
```

确认选项：

- `y`：执行一次
- `n`：跳过并返回失败

> **注意**：`execute_shell` 的 `"a"`（永远放行）选项始终禁用，每次危险 shell 仍需单独 `y/n` 确认。

### 5.2 YOLO 模式（`confirm_dangerous = false`，默认）

- 危险操作不询问用户，直接执行
- 仍记录安全日志
- `execute_shell` 的 `"a"` 选项同样禁用

### 5.3 切换命令

REPL 中输入 `/yolo` 可在安全模式与 YOLO 模式之间切换：

```
coding-agent> /yolo
已切换到 安全 模式

coding-agent> /yolo
已切换到 YOLO 模式
```

## 6. 日志记录

- 所有危险操作记录到 `~/.coding-agent/coding-agent.log`
- 记录内容：时间、工具名、参数、用户是否确认、结果

## 7. 多 Agent 场景下的安全

- Worker 继承 Supervisor 的 `SecurityConfig`
- Worker 的危险操作确认由 Supervisor 代理
- Worker 进程的 `cwd` 限制在 workspace
- Worker 不能访问 `~/.coding-agent` 等敏感目录

## 8. 测试用例

### 路径边界

| 用例 | 输入 | 预期结果 |
|---|---|---|
| 工作目录内 | `path="src/main.py"` | 通过 |
| 当前目录 | `path="./main.py"` | 通过 |
| 越界 | `path="../secret.txt"` | 拒绝 |
| 软链越界 | 文件在工作目录内但软链指向外部 | 拒绝 |
| 绝对路径 | `path="/etc/passwd"` | 拒绝 |

### Shell 分类

| 用例 | 命令 | 预期分类 |
|---|---|---|
| 只读 | `ls -la` | harmless |
| 读取 + 过滤 | `cat a.py \| grep def` | harmless |
| 写操作 | `echo x > a.py` | dangerous |
| 组合命令 | `ls && rm a.py` | dangerous |
| 安装 | `pip install requests` | dangerous |
| 网络 | `curl https://example.com` | dangerous |
| 系统级 | `sudo ls` | forbidden |
| 删除根 | `rm -rf /` | forbidden |
| 越界读取 | `cat ../x.txt` | forbidden |

### 确认流程

| 用例 | 用户输入 | 预期行为 |
|---|---|---|
| 确认 | `y` | 执行操作 |
| 拒绝 | `n` | 不执行，返回失败 |
| 无效输入 | `xxx` | 重复询问直到得到 y/n |
| YOLO 模式 | `confirm_dangerous=false` | 直接执行，不询问 |
| `/yolo` 切换 | 输入 `/yolo` | 切换模式 |

### execute_shell 特殊规则

| 用例 | 输入 | 预期行为 |
|---|---|---|
| 安全模式下的 `a` | 输入 `a` | 拒绝，要求输入 y/n |
| YOLO 模式下的 `a` | 输入 `a` | 拒绝，要求输入 y/n |
