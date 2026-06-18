# Changelog

## [0.2.0] - 2026-06-16

### 新增

- 多 Agent 架构（P5 Phase 1）：Supervisor-Worker 进程级并行、`/goals` 目标管理、REPL `/agent` 角色切换
- Unix Domain Socket IPC，支持多 Worker 并发连接；心跳与看门狗超时机制
- Goal SQLite 持久化，支持状态机、依赖、角色隔离和数据库 schema 迁移
- 6 个内置角色：`default`、`architect`、`coder`、`reviewer`、`tester`、`git`
- 角色模型覆盖和配置快照下发，Worker 使用 Supervisor 的 config
- 每个 Worker 独立日志文件：`~/.coding-agent/workers/<goal_id>.log`
- REPL `/yolo on|off|status` 显式控制危险操作确认模式
- 多文件编辑工具 `read_multiple_files` 和 `apply_patch`，支持跨文件重构与原子回滚
- 基于 tree-sitter 的 Python 代码索引模块，支持自动构建和增量更新
- 语义搜索工具 `symbol_search`、`find_definition`、`find_references`
- REPL `/index` 命令用于手动重建代码索引
- 新增约 50 个测试，测试总数达到 297+

## [0.1.0] - 2026-06-15

### 新增

- 独立的命令行 AI 编程助手 MVP
- REPL 交互界面，支持快捷命令 `/help`、`/clear`、`/model`
- 11 个内置工具：read_file、write_file、str_replace_file、execute_shell、list_directory、glob_search、code_search、web_search、fetch_url、ask_user、set_todo
- 白名单安全策略：危险操作需要用户确认，禁止访问工作目录外路径
- SQLite 历史持久化：会话消息和待办事项自动保存
- Kimi / OpenAI 双模型后端切换
- 189 个单元测试和 6 个端到端测试
- GitHub Actions CI 工作流
