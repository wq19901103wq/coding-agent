"""Tests for RoleLoader edge cases."""

import tempfile
from pathlib import Path

from agent.supervisor.role_loader import RoleLoader


def test_role_loader_skips_invalid_yaml():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "valid.yaml").write_text(
            "name: valid\n"
            "description: A valid role\n"
            "system_prompt: You are valid.\n",
            encoding="utf-8",
        )
        (tmp_path / "invalid.yaml").write_text(
            "name: invalid\n  bad_indent:\n",
            encoding="utf-8",
        )
        loader = RoleLoader(str(tmp_path))
        assert "valid" in loader.list_roles()
        assert "invalid" not in loader.list_roles()
