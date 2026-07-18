import json
import os
import shlex
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from agent.config import Config
from scripts.compare_three_systems import (
    ComparisonResult,
    _activate_command_venv,
    _claude_environment,
    build_goal_description,
    has_existing_patch,
    preflight_claude_endpoint,
    reevaluate_existing_patch,
    render_table,
    run_direct,
    save_comparison,
    save_system_result,
    saved_evaluation_infrastructure_error,
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


@pytest.mark.parametrize(
    "output",
    [
        "/eval.sh: line 89: pytest: command not found",
        "ERROR: Could not find a version that satisfies the requirement setuptools>=40.0",
        "ERROR: No matching distribution found for setuptools>=40.0",
        "packaging.version.InvalidVersion: Invalid version: 'unknown'",
        "ModuleNotFoundError: No module named '_pytest._version'",
        "ModuleNotFoundError: No module named 'setuptools.dep_util'",
        "ModuleNotFoundError: No module named 'extension_helpers'",
        "UserWarning: could not determine astropy package version; "
        "this indicates a broken installation",
    ],
)
def test_docker_evaluator_detects_harness_failures(output):
    pytest.importorskip("docker")
    pytest.importorskip("swebench")
    from swe_bench.docker import detect_infrastructure_failure

    assert detect_infrastructure_failure(output)


def test_docker_evaluator_does_not_misclassify_test_failure():
    pytest.importorskip("docker")
    pytest.importorskip("swebench")
    from swe_bench.docker import detect_infrastructure_failure

    assert detect_infrastructure_failure("FAILED tests/test_feature.py::test_answer") is None


def test_saved_evaluation_infrastructure_error_reads_legacy_log(tmp_path):
    log = tmp_path / "claude" / "docker_eval" / "task" / "test_output.txt"
    log.parent.mkdir(parents=True)
    log.write_text("ModuleNotFoundError: No module named '_pytest._version'")

    error = saved_evaluation_infrastructure_error(tmp_path, "claude")

    assert error is not None
    assert "infrastructure failure" in error


def test_saved_evaluation_uses_only_latest_log(tmp_path):
    old_log = tmp_path / "claude" / "docker_eval" / "task" / "test_output.txt"
    old_log.parent.mkdir(parents=True)
    old_log.write_text("ModuleNotFoundError: No module named '_pytest._version'")

    new_log = tmp_path / "claude" / "docker_reeval" / "task" / "test_output.txt"
    new_log.parent.mkdir(parents=True)
    new_log.write_text("FAILED tests/test_feature.py::test_answer")
    new_time = old_log.stat().st_mtime + 1
    os.utime(new_log, (new_time, new_time))

    assert saved_evaluation_infrastructure_error(tmp_path, "claude") is None


def test_has_existing_patch_rejects_empty_checkpoint(tmp_path):
    patch = tmp_path / "claude" / "agent.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("\n")
    assert not has_existing_patch(tmp_path, "claude", "task")

    patch.write_text("diff --git a/a.py b/a.py\n")
    assert has_existing_patch(tmp_path, "claude", "task")


def test_comparison_checkpoints_infrastructure_failure_without_counting_wrong(tmp_path):
    result = ComparisonResult(
        task_id="demo",
        direct_resolved=None,
        direct_duration=None,
        direct_error=None,
        claude_resolved=None,
        claude_duration=None,
        claude_error=None,
        swe_agent_resolved=None,
        swe_agent_duration=None,
        swe_agent_error=None,
    )
    result.claude_status = "running"
    save_comparison(tmp_path / "comparison.json", result)
    save_system_result(
        result,
        "claude",
        {"resolved": False, "duration": 12.5, "error": "infrastructure failure"},
        infrastructure_failure=True,
    )
    save_comparison(tmp_path / "comparison.json", result)

    saved = json.loads((tmp_path / "comparison.json").read_text())
    assert saved["claude_resolved"] is None
    assert saved["claude_status"] == "infrastructure_error"
    assert saved["claude_duration"] == 12.5


def test_comparison_report_excludes_unfinished_results_from_denominator():
    result = ComparisonResult(
        task_id="demo",
        direct_resolved=True,
        direct_duration=1.0,
        direct_error=None,
        claude_resolved=None,
        claude_duration=1.0,
        claude_error="infrastructure failure",
        swe_agent_resolved=None,
        swe_agent_duration=None,
        swe_agent_error=None,
        direct_status="completed",
        claude_status="infrastructure_error",
    )

    report = render_table([result])

    assert "| demo | True | infra | - |" in report
    assert "**direct resolved:** 1/1 completed (0 unfinished)" in report
    assert "**Claude resolved:** 0/0 completed (1 unfinished)" in report


def test_infrastructure_retry_reuses_saved_patch_without_model_call(tmp_path, monkeypatch):
    patch_dir = tmp_path / "claude"
    patch_dir.mkdir()
    (patch_dir / "agent.patch").write_text("diff --git a/a.py b/a.py\n")
    observed = {}

    def fake_evaluate(task, workspace, patch, output_dir):
        observed.update(
            task=task,
            workspace=workspace,
            patch=patch,
            output_dir=output_dir,
        )
        return {"resolved": True, "duration": None, "error": None, "patch": patch}

    monkeypatch.setattr("scripts.compare_three_systems.evaluate_patch", fake_evaluate)
    workspace = tmp_path / "workspace"
    outcome = reevaluate_existing_patch(_task(), tmp_path, "claude", workspace, 42.0)

    assert outcome is not None
    assert outcome["resolved"] is True
    assert outcome["duration"] == 42.0
    assert observed["patch"] == "diff --git a/a.py b/a.py\n"
    assert observed["output_dir"] == patch_dir / "docker_reeval"


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
        assert env.communicate('printf "$PIP_REQUIRE_VIRTUALENV"') == "true"
        assert env.communicate('printf "$PYTHONNOUSERSITE"') == "1"
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


def test_claude_command_venv_blocks_global_pip(tmp_path):
    command_venv = tmp_path / "command_venv"

    env = _activate_command_venv({"PATH": "/usr/bin"}, command_venv)

    assert env["VIRTUAL_ENV"] == str(command_venv)
    assert env["PATH"].startswith(f"{command_venv / 'bin'}{os.pathsep}")
    assert env["PIP_REQUIRE_VIRTUALENV"] == "true"
    assert env["PYTHONNOUSERSITE"] == "1"


def test_claude_preflight_allows_slow_official_endpoint(monkeypatch):
    observed = {}

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    monkeypatch.setattr("scripts.compare_three_systems.subprocess.run", fake_run)

    preflight_claude_endpoint("deepseek-v4-flash")

    assert observed["timeout"] == 300


def test_direct_benchmark_uses_explicit_fair_limits(monkeypatch, tmp_path):
    observed = {}

    class FakeRunner:
        def __init__(self, *, config, **_kwargs):
            observed["model"] = config.llm.model
            observed["max_steps"] = config.llm.max_steps_per_turn
            observed["token_budget"] = config.llm.max_total_tokens_per_turn

        def run_task(self, _task):
            return SimpleNamespace(resolved=False, patch_path=None, error="expected")

    runner_module = ModuleType("swe_bench.runner")
    runner_module.SWEBenchRunner = FakeRunner  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "swe_bench.runner", runner_module)
    config = Config()
    original = (
        config.llm.model,
        config.llm.max_steps_per_turn,
        config.llm.max_total_tokens_per_turn,
    )

    result = run_direct(
        _task(),
        tmp_path / "output",
        tmp_path / "workspace",
        config,
        "deepseek-v4-flash",
        max_steps=100,
        token_budget=1_000_000,
    )

    assert result["error"] == "expected"
    assert observed == {
        "model": "deepseek-v4-flash",
        "max_steps": 100,
        "token_budget": 1_000_000,
    }
    assert (
        config.llm.model,
        config.llm.max_steps_per_turn,
        config.llm.max_total_tokens_per_turn,
    ) == original


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


def test_local_fallback_patches_repo_install_without_global_opt_in(monkeypatch):
    monkeypatch.delenv("SWE_BENCH_PATCH_EVAL_ENV", raising=False)
    evaluator = _docker_evaluator()

    patched = evaluator._prepare_eval_script(
        "#!/bin/bash\npython -m pip install -e .\n", local_fallback=True
    )

    assert "python -m pip install -e . --no-build-isolation" in patched


def test_local_fallback_bootstraps_testbed_build_requirements(monkeypatch):
    monkeypatch.delenv("SWE_BENCH_PATCH_EVAL_ENV", raising=False)
    evaluator = _docker_evaluator()
    commands = []

    class FakeContainer:
        def exec_run(self, command, **_kwargs):
            commands.append(command)
            return SimpleNamespace(exit_code=0, output=b"")

    evaluator._configure_container_pip(FakeContainer(), required=True)

    assert commands
    assert all(
        command.startswith("/opt/miniconda3/envs/testbed/bin/python") for command in commands
    )
    assert any("setuptools_scm<8" in command for command in commands)
    assert any("extension-helpers" in command for command in commands)


def test_astropy_fallback_installs_pinned_cython(monkeypatch):
    monkeypatch.delenv("SWE_BENCH_PATCH_EVAL_ENV", raising=False)
    evaluator = _docker_evaluator()
    evaluator.task.repo = "astropy/astropy"
    commands = []

    class FakeContainer:
        def exec_run(self, command, **_kwargs):
            commands.append(command)
            return SimpleNamespace(exit_code=0, output=b"")

    evaluator._configure_container_pip(FakeContainer(), required=True)

    assert any("cython==0.29.22" in command for command in commands)


def test_docker_eval_patch_keeps_shell_operators_valid():
    evaluator = _docker_evaluator(patch_eval_environment=True)
    script = "python -m pip install -e . || true\n# pip install example\npip install \\\n"

    patched = evaluator._prepare_eval_script(script)

    assert "python -m pip install -e . --no-build-isolation || true" in patched
    assert "# pip install example" in patched
    assert patched.endswith("pip install \\")
