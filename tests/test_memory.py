import stat
from pathlib import Path

import pytest

from agent.memory import MemoryManager
from agent.tools.base import ToolContext
from agent.tools.project_memory import RememberProjectMemoryTool


def _manager(tmp_path: Path, workspace: Path | None = None, **kwargs) -> MemoryManager:
    return MemoryManager(
        workspace or tmp_path / "workspace",
        storage_root=tmp_path / "private",
        **kwargs,
    )


def test_loads_shared_instructions_and_private_memory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Use pytest.", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("Keep changes small.", encoding="utf-8")
    memory = _manager(tmp_path, workspace)
    memory.add("The API package is agent/llm")

    context = memory.render_context()

    assert "Use pytest." in context
    assert "Keep changes small." in context
    assert "The API package is agent/llm" in context


def test_private_memory_is_outside_workspace_and_private(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory = _manager(tmp_path, workspace)

    memory.add("Run focused tests first")

    assert workspace not in memory.private_path.parents
    assert stat.S_IMODE(memory.private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(memory.private_path.parent.stat().st_mode) == 0o700
    assert memory.private_path.name == "MEMORY.md"


def test_add_deduplicates_and_remove_uses_display_index(tmp_path):
    memory = _manager(tmp_path)

    assert memory.add("Use   Python 3.11") is True
    assert memory.add("Use Python 3.11") is False
    assert memory.list_entries() == ["Use Python 3.11"]
    assert memory.remove(1) == "Use Python 3.11"
    assert memory.list_entries() == []


@pytest.mark.parametrize(
    "value",
    [
        "api_key=abcdefghijklmnop",
        "password: very-secret-value",
        "Authorization: Bearer abcdef",
        "sk-abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_rejects_likely_secrets(tmp_path, value):
    memory = _manager(tmp_path)

    with pytest.raises(ValueError, match="密钥或密码"):
        memory.add(value)


def test_render_context_is_bounded(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("x" * 500, encoding="utf-8")
    memory = _manager(tmp_path, workspace, max_chars=100)

    context = memory.render_context()

    assert len(context) <= 100
    assert "截断" in context


def test_render_context_counts_separators_between_sources(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("a" * 60, encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("b" * 60, encoding="utf-8")
    memory = _manager(tmp_path, workspace, max_chars=100)

    assert len(memory.render_context()) <= 100


def test_disabled_memory_loads_and_writes_nothing(tmp_path):
    memory = _manager(tmp_path, enabled=False)

    assert memory.render_context() == ""
    assert memory.load_sources() == []


def test_auto_memory_tool_writes_without_confirmation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = tmp_path / "private"
    tool = RememberProjectMemoryTool()
    ctx = ToolContext(
        workspace=str(workspace),
        config={
            "memory": {
                "enabled": True,
                "auto_save": True,
                "max_chars": 1000,
                "storage_root": str(storage),
            }
        },
    )

    result = tool.execute(
        {"fact": "The focused test command is pytest -q tests/test_memory.py"}, ctx
    )

    assert result.success
    memory = MemoryManager(workspace, storage_root=storage, max_chars=1000)
    assert memory.list_entries() == ["The focused test command is pytest -q tests/test_memory.py"]


def test_auto_memory_tool_respects_auto_save_toggle(tmp_path):
    tool = RememberProjectMemoryTool()
    ctx = ToolContext(
        workspace=str(tmp_path),
        config={"memory": {"enabled": True, "auto_save": False}},
    )

    result = tool.execute({"fact": "Remember this"}, ctx)

    assert not result.success
    assert "自动记忆已关闭" in (result.error or "")
