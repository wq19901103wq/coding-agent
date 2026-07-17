import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.compare_three_systems import (
    _claude_environment,
    build_goal_description,
    preflight_claude_endpoint,
)
from swe_agent_local_runner import (
    LocalSWEEnv,
    _install_local_environment_stubs,
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


def test_local_env_uses_per_task_command_venv(tmp_path):
    command_venv = tmp_path / "command_venv"
    (command_venv / "bin").mkdir(parents=True)
    env = LocalSWEEnv(tmp_path, _task(), timeout=5, command_venv=command_venv)
    try:
        assert env.communicate('printf "$VIRTUAL_ENV"') == str(command_venv)
        path = env.communicate('printf "$PATH"')
        assert path.split(":")[1] == str(command_venv / "bin")
    finally:
        env.close()


def test_local_runner_stubs_docker_only_swe_agent_modules(monkeypatch):
    monkeypatch.delitem(sys.modules, "sweagent.environment.swe_env", raising=False)
    monkeypatch.delitem(sys.modules, "sweagent.environment.utils", raising=False)

    _install_local_environment_stubs()

    assert sys.modules["sweagent.environment.swe_env"].SWEEnv is object
    copy = sys.modules["sweagent.environment.utils"].copy_anything_to_container
    with pytest.raises(RuntimeError, match="unavailable in local SWE-agent mode"):
        copy(None, "source", "destination")


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
    prompt = build_goal_description(_task(hints_text="private implementation hint"))

    assert "hidden/test_secret.py" not in prompt
    assert "FAIL_TO_PASS" not in prompt
    assert "private implementation hint" not in prompt
    assert "public issue description" in prompt


def test_claude_environment_can_use_shared_benchmark_endpoint(monkeypatch):
    monkeypatch.setenv("SWE_BENCH_CLAUDE_BASE_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setenv("SWE_BENCH_CLAUDE_API_KEY", "shared-secret")

    env = _claude_environment("deepseek-v4-flash")

    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "shared-secret"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "shared-secret"
    assert env["ANTHROPIC_MODEL"] == "deepseek-v4-flash[1m]"


def test_claude_preflight_allows_slow_official_endpoint(monkeypatch):
    observed = {}

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    monkeypatch.setattr("scripts.compare_three_systems.subprocess.run", fake_run)

    preflight_claude_endpoint("deepseek-v4-flash")

    assert observed["timeout"] == 300


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
