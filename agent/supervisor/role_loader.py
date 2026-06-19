"""Load agent role definitions from YAML files."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from agent.supervisor.models import AgentRole

DEFAULT_ROLES = {
    "default": AgentRole(
        name="default",
        description="通用 coding agent，处理日常编码任务",
        system_prompt=("你是一个命令行 AI 编程助手。请使用工具完成用户的任务。"),
    ),
    "architect": AgentRole(
        name="architect",
        description="负责设计、规划和代码审查，只读工具为主",
        system_prompt=(
            "你是一个软件架构师。你只使用只读工具分析项目，输出设计方案和修改建议，不直接修改文件。"
        ),
        allowed_tools=[
            "read_file",
            "read_multiple_files",
            "list_directory",
            "glob_search",
            "code_search",
            "symbol_search",
            "find_definition",
            "find_references",
            "ask_user",
            "set_todo",
        ],
    ),
    "coder": AgentRole(
        name="coder",
        description="实现代码、写测试、运行 shell",
        system_prompt=(
            "你是一个专注实现的开发工程师。你可以读写文件、执行 shell 和测试，但不能提交 git。"
        ),
        allowed_tools=[
            "read_file",
            "read_multiple_files",
            "write_file",
            "str_replace_file",
            "apply_patch",
            "execute_shell",
            "list_directory",
            "glob_search",
            "code_search",
            "symbol_search",
            "find_definition",
            "find_references",
            "ask_user",
            "set_todo",
        ],
        forbidden_tools=["git_commit"],
    ),
    "reviewer": AgentRole(
        name="reviewer",
        description="代码审查、找 bug、提建议",
        system_prompt=("你是一个代码审查者。你只使用只读工具检查代码，输出审查意见。"),
        allowed_tools=[
            "read_file",
            "read_multiple_files",
            "list_directory",
            "glob_search",
            "code_search",
            "symbol_search",
            "find_definition",
            "find_references",
            "ask_user",
            "set_todo",
        ],
    ),
    "tester": AgentRole(
        name="tester",
        description="运行测试、验证修复",
        system_prompt=(
            "你是一个测试工程师。你可以运行 shell 命令执行测试，并读取相关文件验证结果。"
        ),
        allowed_tools=[
            "read_file",
            "read_multiple_files",
            "execute_shell",
            "list_directory",
            "glob_search",
            "code_search",
            "symbol_search",
            "find_definition",
            "find_references",
            "ask_user",
            "set_todo",
        ],
    ),
    "git": AgentRole(
        name="git",
        description="Git 操作专家",
        system_prompt=(
            "你是一个 Git 操作专家。"
            "你可以执行 git 相关 shell 命令和读取文件，但不直接修改业务代码。"
        ),
        allowed_tools=[
            "read_file",
            "execute_shell",
            "list_directory",
            "ask_user",
            "set_todo",
        ],
    ),
}


class RoleLoader:
    """Load and cache agent roles from a directory of YAML files."""

    def __init__(self, roles_dir: str | None = None):
        if roles_dir is None:
            roles_dir = os.path.join(os.path.dirname(__file__), "..", "..", "agents")
        self.roles_dir = Path(roles_dir).resolve()
        self._roles: dict[str, AgentRole] | None = None

    def load_all(self) -> dict[str, AgentRole]:
        if self._roles is not None:
            return dict(self._roles)

        file_roles: dict[str, AgentRole] = {}
        has_files = False

        if self.roles_dir.exists():
            for path in sorted(self.roles_dir.glob("*.yaml")):
                has_files = True
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    role = AgentRole(**data)
                    file_roles[role.name] = role
                except Exception:
                    # Ignore malformed role files silently.
                    continue

        if has_files:
            roles = file_roles
        else:
            roles = dict(DEFAULT_ROLES)

        self._roles = roles
        return dict(roles)

    def get(self, name: str) -> AgentRole:
        roles = self.load_all()
        if name not in roles:
            raise KeyError(f"Role '{name}' not found")
        return roles[name]

    def list_roles(self) -> list[str]:
        return list(self.load_all().keys())
