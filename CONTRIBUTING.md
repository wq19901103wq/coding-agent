# 贡献指南

感谢你对 coding-agent 的兴趣！

## 开发环境

```bash
git clone https://github.com/wq19901103wq/coding-agent.git
cd coding-agent
pip install -e ".[dev]"
```

## 代码规范

- Python 3.10+ 语法
- 使用 `ruff` 进行代码格式化和检查
- 使用 `mypy` 进行类型检查
- 使用 `pytest` 编写测试

## 提交前检查

```bash
ruff format --check
ruff check
mypy agent tests
python -m pytest
```

## 使用 pre-commit（推荐）

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

安装后，每次 `git commit` 会自动运行格式化、类型检查和测试。

## 提交规范

| 前缀 | 用途 |
|---|---|
| `feat:` | 新功能 |
| `fix:` | 修复 bug |
| `test:` | 测试相关 |
| `docs:` | 文档相关 |
| `refactor:` | 重构 |
| `ci:` | CI/CD 相关 |
| `chore:` | 构建/工具/依赖 |

## PR 流程

1. 从 `main` 切出功能分支：`git checkout -b feat/xxx`
2. 提交前确保 `ruff`、`mypy`、`pytest` 全部通过
3. 推送分支并创建 Pull Request
4. 填写 PR 模板中的变更说明和检查清单
5. CI 通过后使用 **Squash and Merge** 合并

## 代码审查要求

- 每次 PR 都需要通过 CI 检查
- 个人项目不强制要求他人 review，但建议核心功能变更进行自我审查
- 合并前确保 CHANGELOG 和 README 已同步更新

## 报告问题

请在 [Issues](https://github.com/wq19901103wq/coding-agent/issues) 中描述问题，并尽量提供复现步骤。
