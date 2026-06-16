# Changelog

## [0.2.0] - 2026-06-16

### 新增

- 多文件编辑工具 `read_multiple_files` 和 `apply_patch`，支持跨文件重构与原子回滚
- 基于 tree-sitter 的 Python 代码索引模块，支持自动构建和增量更新
- 语义搜索工具 `symbol_search`、`find_definition`、`find_references`
- REPL `/index` 命令用于手动重建代码索引
- 新增 15 个测试，测试总数达到 204

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
