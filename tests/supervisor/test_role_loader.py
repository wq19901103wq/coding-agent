"""Tests for role loader."""

import pytest
import yaml

from agent.supervisor.role_loader import RoleLoader


@pytest.fixture
def roles_dir(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "coder.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "coder",
                "description": "Writes code",
                "system_prompt": "You are a coder.",
                "allowed_tools": ["read_file", "write_file"],
                "forbidden_tools": ["git_commit"],
                "model": "kimi-for-coding",
                "max_steps_per_turn": 100,
                "temperature": 0.7,
            }
        )
    )
    (d / "reviewer.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "reviewer",
                "description": "Reviews code",
                "system_prompt": "You are a reviewer.",
                "allowed_tools": ["read_file"],
            }
        )
    )
    return d


def test_load_all_roles(roles_dir):
    loader = RoleLoader(str(roles_dir))
    roles = loader.load_all()
    assert "coder" in roles
    assert "reviewer" in roles
    assert roles["coder"].description == "Writes code"
    assert roles["reviewer"].allowed_tools == ["read_file"]


def test_get_role(roles_dir):
    loader = RoleLoader(str(roles_dir))
    role = loader.get("coder")
    assert role.name == "coder"
    assert role.allowed_tools == ["read_file", "write_file"]
    assert role.forbidden_tools == ["git_commit"]
    assert role.model == "kimi-for-coding"


def test_get_missing_role(roles_dir):
    loader = RoleLoader(str(roles_dir))
    with pytest.raises(KeyError):
        loader.get("architect")


def test_default_roles_exist():
    loader = RoleLoader()
    roles = loader.load_all()
    expected = {"default", "architect", "coder", "reviewer", "tester", "git"}
    assert expected.issubset(set(roles.keys()))


def test_default_coder_role():
    loader = RoleLoader()
    coder = loader.get("coder")
    assert coder.name == "coder"
    assert "write_file" in (coder.allowed_tools or [])
    assert "git_commit" in coder.forbidden_tools
