import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.compare_three_systems import build_goal_description
from swe_agent_local_runner import (
    LocalSWEEnv,
    _materialize_agent_config,
    _review_and_cleanup_changes,
)


def _task(**overrides):
    values = {
        "id": "demo__task-1",
        "issue_title": "Fix behavior",
        "issue_body": "The public issue description.",
        "hints_text": "",
        "fail_to_pass": ["hidden/test_secret.py::test_answer"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _docker_evaluator(**kwargs):
    pytest.importorskip("docker")
    pytest.importorskip("swebench")
    from swe_bench.docker import DockerEvaluator

    return DockerEvaluator(_task(), **kwargs)


def test_local_env_preserves_command_exit_status(tmp_path):
    env = LocalSWEEnv(tmp_path, _task(), timeout=5)
    try:
        assert env.communicate("false") == ""
        assert env.returncode == 1

        assert env.communicate("printf success") == "success"
        assert env.returncode == 0
    finally:
        env.close()


def test_inline_state_command_returns_json(tmp_path):
    config_path = Path("swe_agent_local_config/default_local.yaml")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    state_command = config["state_command"]["name"].strip()

    env = LocalSWEEnv(tmp_path, _task(), timeout=5)
    try:
        state = json.loads(env.communicate(state_command))
        assert state["working_dir"] == str(tmp_path)
        assert state["open_file"] == "n/a"
    finally:
        env.close()


def test_communicate_with_handling_uses_requested_timeout(tmp_path):
    env = LocalSWEEnv(tmp_path, _task(), timeout=5)
    try:
        output = env.communicate_with_handling("sleep 2", timeout_duration=1)
        assert "timed out after 1s" in output
        assert env.communicate("printf restarted") == "restarted"
    finally:
        env.close()


def test_cleanup_only_reverts_exact_git_paths():
    changed_path = "src/name with `shell syntax`.py"

    class FakeEnv:
        query_count = 0

        def __init__(self):
            self.commands = []

        def communicate(self, command):
            self.commands.append(command)
            if command == "git status --short":
                return f" M {changed_path}"
            if command == "git diff --stat HEAD":
                return f" {changed_path} | 1 +"
            if command == "git diff --name-only HEAD":
                return changed_path
            return ""

    class FakeModel:
        def query(self, _history):
            return f"not/actually/changed.py\n{changed_path}"

    env = FakeEnv()
    _review_and_cleanup_changes(env, FakeModel(), "issue")

    restore_commands = [c for c in env.commands if c.startswith("git restore")]
    assert restore_commands == [
        "git restore --source=HEAD --staged --worktree -- " + shlex.quote(changed_path)
    ]


def test_runtime_config_resolves_relative_command_files(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source = config_dir / "agent.yaml"
    source.write_text("command_files:\n  - commands.sh\n", encoding="utf-8")

    runtime = _materialize_agent_config(source, tmp_path)
    data = yaml.safe_load(runtime.read_text(encoding="utf-8"))

    assert data["command_files"] == [str((config_dir / "commands.sh").resolve())]


def test_comparison_prompt_does_not_expose_hidden_tests():
    prompt = build_goal_description(_task())

    assert "hidden/test_secret.py" not in prompt
    assert "FAIL_TO_PASS" not in prompt
    assert "public issue description" in prompt


def test_docker_evaluator_does_not_rewrite_official_script_by_default(monkeypatch):
    monkeypatch.delenv("SWE_BENCH_PATCH_EVAL_ENV", raising=False)
    evaluator = _docker_evaluator()
    script = "#!/bin/bash\npython -m pip install -e .\n"

    assert evaluator._prepare_eval_script(script) == script


def test_docker_eval_compatibility_patch_is_explicit(monkeypatch):
    monkeypatch.setenv("SWE_BENCH_PATCH_EVAL_ENV", "true")
    evaluator = _docker_evaluator()

    patched = evaluator._prepare_eval_script("#!/bin/bash\npython -m pip install -e .\n")

    assert "python -m pip install -e . --no-build-isolation" in patched


def test_docker_eval_patch_keeps_shell_operators_valid():
    evaluator = _docker_evaluator(patch_eval_environment=True)
    script = "python -m pip install -e . || true\n# pip install example\npip install \\\n"

    patched = evaluator._prepare_eval_script(script)

    assert "python -m pip install -e . --no-build-isolation || true" in patched
    assert "# pip install example" in patched
    assert patched.endswith("pip install \\")
