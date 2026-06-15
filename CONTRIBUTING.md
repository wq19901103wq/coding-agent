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
ruff format agent tests main.py
ruff check agent tests main.py
mypy agent tests
python -m pytest
```

## 提交规范

- `feat:` 新功能
- `fix:` 修复 bug
- `test:` 测试相关
- `docs:` 文档相关
- `refactor:` 重构
- `ci:` CI/CD 相关

## 报告问题

请在 [Issues](https://github.com/wq19901103wq/coding-agent/issues) 中描述问题，并尽量提供复现步骤。
