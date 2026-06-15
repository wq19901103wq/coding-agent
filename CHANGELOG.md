# Changelog

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
