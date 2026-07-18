#!/usr/bin/env python3
"""Run SWE-agent v0.7.0's Agent loop locally (without its Docker container).

This is a "semi-official" SWE-agent setup: we reuse SWE-agent's Agent,
prompts, and bash commands, but execute them in a persistent local bash
session instead of inside `sweagent/swe-agent:latest`. Patches are saved to
`output_dir/agent.patch`; the caller must evaluate them in the coding-agent
environment because of swebench version conflicts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import yaml

# Add SWE-agent source to path. Keep a local copy under .vendor so /tmp
# cleanups do not break the runner.
SWE_AGENT_ROOT = Path(
    os.environ.get(
        "SWE_AGENT_ROOT",
        Path(__file__).resolve().parent / ".vendor" / "SWE-agent-0.7.0",
    )
).resolve()
if SWE_AGENT_ROOT.exists():
    sys.path.insert(0, str(SWE_AGENT_ROOT))
    # An editable install pointing elsewhere must not shadow the selected
    # source tree. If no source tree is present, keep normal installed-package
    # discovery intact.
    sys.meta_path = [f for f in sys.meta_path if type(f).__name__ != "_EditableFinder"]


def _install_local_environment_stubs() -> None:
    """Stub Docker-only imports that the local runner never executes."""
    swe_env_module = "sweagent.environment.swe_env"
    if swe_env_module not in sys.modules:
        stub = ModuleType(swe_env_module)
        stub.SWEEnv = object  # type: ignore[attr-defined]
        sys.modules[swe_env_module] = stub

    utils_module = "sweagent.environment.utils"
    if utils_module not in sys.modules:
        utils_stub = ModuleType(utils_module)

        def unavailable_copy(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("container file copying is unavailable in local SWE-agent mode")

        utils_stub.copy_anything_to_container = unavailable_copy  # type: ignore[attr-defined]
        sys.modules[utils_module] = utils_stub


def _load_sweagent_classes():
    """Load optional SWE-agent dependencies without Docker/dataset imports."""
    try:
        import anyio.to_thread  # noqa: F401
        import together  # type: ignore[import-not-found]  # noqa: F401

        # LocalSWEEnv and the configured Identity summarizer make these
        # Docker-only modules unnecessary. Their real imports pull in
        # swebench/datasets/numpy and can add minutes of irrelevant startup.
        _install_local_environment_stubs()
        from sweagent.agent.agents import (  # type: ignore[import-not-found]
            Agent,
            AgentArguments,
        )
        from sweagent.agent.models import ModelArguments  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "SWE-agent dependencies are unavailable. Run this script with the "
            "configured SWE_AGENT_ENV interpreter and install its declared dependencies."
        ) from exc
    return Agent, AgentArguments, ModelArguments


logger = logging.getLogger("swe_agent_local_runner")

LOCAL_CONFIG_DIR = Path(__file__).resolve().parent / "swe_agent_local_config"
BASH_BIN = os.environ.get("SWE_AGENT_BASH") or shutil.which("bash") or "/bin/bash"


@dataclass
class SWEAgentLocalResult:
    task_id: str
    resolved: bool
    duration_seconds: float
    patch: str
    error: str | None


class LocalSWEEnv:
    """Persistent-bash SWEEnv-compatible environment that runs in a local workspace."""

    name = "swe_local"

    def __init__(
        self,
        workspace: Path,
        task: Any,
        command_files: list[Path] | None = None,
        timeout: int = 120,
        command_venv: Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.task = task
        self.timeout = timeout
        self.command_venv = command_venv
        self.communicate_output: str | None = None
        self.returncode: int | None = None
        self.container_obj = SimpleNamespace(id="local")
        self.challenge = None

        self.query = ""
        if hasattr(task, "issue_title") and task.issue_title:
            self.query += f"{task.issue_title}\n"
        if hasattr(task, "issue_body") and task.issue_body:
            body = task.issue_body
            max_body = 6000
            if len(body) > max_body:
                body = body[:max_body] + "\n... [issue body truncated]\n"
            self.query += body

        self.record: dict[str, Any] = {
            "instance_id": getattr(task, "id", "unknown"),
        }

        if command_files is None:
            command_files = [
                LOCAL_CONFIG_DIR / "defaults.sh",
                LOCAL_CONFIG_DIR / "search.sh",
                LOCAL_CONFIG_DIR / "edit_linting.sh",
                LOCAL_CONFIG_DIR / "_split_string.py",
                LOCAL_CONFIG_DIR / "submit.sh",
            ]
        self.command_files = command_files

        self._commands_dir = self.workspace / ".swe_agent" / "commands"
        self._backup_dir = self.workspace / ".swe_agent" / "backup"
        self._init_script = self.workspace / ".swe_agent" / "init.sh"
        self._build_init_script()

        self._proc: subprocess.Popen | None = None
        self._start_bash()

    def _build_init_script(self) -> None:
        self._commands_dir.mkdir(parents=True, exist_ok=True)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = [
            "#!/usr/bin/env bash",
            f'export ROOT="{self.workspace}"',
            'export CURRENT_FILE=""',
            "export CURRENT_LINE=0",
            "export SEARCH_RESULTS=()",
            "export SEARCH_FILES=()",
            "export SEARCH_INDEX=0",
            "export WINDOW=${WINDOW:-100}",
            f'export PATH="{self._commands_dir}:$PATH"',
            "",
        ]
        for cf in self.command_files:
            if cf.suffix == ".sh":
                lines.append(f'source "{cf}"')
            elif cf.suffix == ".py":
                dest = self._commands_dir / cf.name
                shutil.copy2(cf, dest)
                dest.chmod(0o755)
        self._init_script.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _is_valid_json(text: str) -> bool:
        try:
            json.loads(text)
            return True
        except json.JSONDecodeError:
            return False

    def _restart_bash(self) -> None:
        """Kill the current bash session (if still alive) and start a fresh one."""
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    pgid = os.getpgid(self._proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            self._proc = None
        self._start_bash()

    def _start_bash(self) -> None:
        """Start a persistent bash session and source the init script."""
        env = {**os.environ, "ROOT": str(self.workspace)}
        if self.command_venv is not None:
            env["VIRTUAL_ENV"] = str(self.command_venv)
            env["PATH"] = f"{self.command_venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
            env["PIP_REQUIRE_VIRTUALENV"] = "true"
            env["PYTHONNOUSERSITE"] = "1"
        # Use a new session so we can kill the whole process group (bash +
        # any spawned children) when a command times out.
        self._proc = subprocess.Popen(
            [BASH_BIN, "--norc", "--noprofile"],
            cwd=self.workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        # Source init script and echo a marker.
        marker = "__SWE_AGENT_INIT_DONE__"
        assert self._proc.stdin is not None
        self._proc.stdin.write(f'source "{self._init_script}"\n')
        self._proc.stdin.write(f'echo "{marker}"\n')
        self._proc.stdin.flush()
        self._read_until(marker, timeout=30)

    def _read_until(self, marker: str, timeout: float = 120) -> str:
        """Read stdout until marker appears."""
        deadline = time.monotonic() + timeout
        buffer: list[str] = []
        assert self._proc is not None
        assert self._proc.stdout is not None
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if marker in line:
                return "".join(buffer)
            buffer.append(line)
            if not line and self._proc.poll() is not None:
                break
        return "".join(buffer)

    def _send(self, cmd: str, timeout: float | None = None) -> str:
        """Send a command to the persistent bash and read output.

        A watchdog thread kills the bash session if the command does not
        return within ``timeout`` seconds. This prevents runaway commands
        (e.g. ``find | xargs grep`` on a hung file) from blocking the whole
        batch forever.
        """
        if self._proc is None or self._proc.poll() is not None:
            self._start_bash()
        timeout = timeout or self.timeout
        marker = f"__SWE_AGENT_DONE_{time.time()}__"
        # Escape marker in command output just in case.
        full_cmd = cmd.rstrip() + "\n" if not cmd.endswith("\n") else cmd
        # Preserve the command's status before running any marker commands.
        full_cmd += (
            f'__swe_status=$?\nprintf "\\nEXITSTATUS:%s\\n" "$__swe_status"\necho "{marker}"\n'
        )

        killed_by_watchdog = False
        proc_ref = self._proc

        def watchdog() -> None:
            nonlocal killed_by_watchdog
            if proc_ref is not None and proc_ref.poll() is None:
                killed_by_watchdog = True
                try:
                    # Kill the whole process group so grandchildren (e.g.
                    # ``sleep`` spawned by a command) cannot keep the stdout
                    # pipe open and block ``readline()``.
                    pgid = os.getpgid(proc_ref.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    try:
                        proc_ref.kill()
                    except ProcessLookupError:
                        pass

        timer = threading.Timer(timeout, watchdog)
        timer.start()
        try:
            self._proc.stdin.write(full_cmd)  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]
            output = self._read_until(marker, timeout=timeout + 10)
        finally:
            timer.cancel()

        if killed_by_watchdog:
            self._proc = None
            return f"ERROR: command timed out after {timeout}s and was killed by watchdog."

        # Extract EXITSTATUS from the last lines.
        lines = output.splitlines()
        self.returncode = 0
        for line in reversed(lines):
            if line.startswith("EXITSTATUS:"):
                try:
                    self.returncode = int(line.split(":", 1)[1])
                except ValueError:
                    self.returncode = 1
                break
        # Remove the marker and EXITSTATUS lines from output.
        cleaned_lines = []
        for line in lines:
            if marker in line or line.startswith("EXITSTATUS:"):
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip()

    def reset(self, index: int | None = None) -> tuple[str, dict[str, Any]]:
        base_commit = getattr(self.task, "base_commit", None)
        if base_commit:
            self.communicate(f"git checkout -f {base_commit}")
            self.communicate("git clean -fd")
        for p in [self.workspace / "model.patch"]:
            p.unlink(missing_ok=True)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._commands_dir.mkdir(parents=True, exist_ok=True)
        return "", {}

    def communicate(self, input_: str, check: str = "none") -> str:
        output = self._send(input_)
        # state 命令必须返回合法 JSON；一旦 bash 被之前的错误命令污染，
        # 返回会为空或非法，此时重启 bash 并重新 source 命令文件。
        is_state_command = input_.strip() == "state" or input_.lstrip().startswith(
            'CURRENT_FILE="$CURRENT_FILE" python3 -c'
        )
        if is_state_command:
            stripped = output.strip() if output else ""
            is_valid_json = False
            if stripped.startswith("{"):
                try:
                    json.loads(stripped)
                    is_valid_json = True
                except json.JSONDecodeError:
                    pass
            if not is_valid_json:
                logger.warning(
                    "state command returned non-JSON output (%r), restarting bash",
                    output[:200],
                )
                self._restart_bash()
                output = self._send(input_)
                # 如果重启后仍然拿不到合法 JSON，返回一个安全默认值，
                # 避免 forward_model 里 json.loads 直接崩溃。
                if not (
                    output
                    and output.strip().startswith("{")
                    and self._is_valid_json(output.strip())
                ):
                    logger.warning(
                        "state command still invalid after bash restart, using default state"
                    )
                    output = json.dumps(
                        {
                            "open_file": "n/a",
                            "working_dir": str(self.workspace),
                        }
                    )
        self.communicate_output = output
        return output

    def communicate_with_handling(
        self, input_: str, error_msg: str = "", timeout_duration: int | None = None
    ) -> str:
        output = self._send(input_, timeout=timeout_duration or self.timeout)
        self.communicate_output = output
        return output

    def get_available_actions(self) -> list[str]:
        return []

    # 全盘搜索命令会产出海量输出污染 state_command，导致 json.loads 崩溃。
    # 在 env 层硬拦截（prompt 约束 LLM 不一定遵守），返回错误让 agent 换命令。
    # 判断：find/grep -r/rg 的参数里有以 / 或 ~ 开头的路径（绝对路径或家目录）。
    _FULL_FS_SEARCH_PATTERNS = (re.compile(r"\b(?:find|grep|rg)\b[^|;]*?\s(?:/|~)[\w/.\-]*"),)

    def _is_full_fs_search(self, action: str) -> bool:
        return any(p.search(action) for p in self._FULL_FS_SEARCH_PATTERNS)

    def step(self, action: str) -> tuple[str, float, bool, dict[str, Any]]:
        if self._is_full_fs_search(action):
            obs = (
                "ERROR: 全盘搜索命令被拒绝（输出过大会崩溃环境）。"
                "请将搜索范围限定在仓库内，例如 `find . -name ...` 或 `grep -r .`。"
            )
            return obs, 0.0, False, {}
        obs = self.communicate(action)
        done = "submit" in action and "<<SUBMISSION||" in obs
        return obs, 0.0, done, {}

    def add_commands(self, commands: list[dict]) -> None:
        pass

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.stdin.write("exit\n")  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def _review_and_cleanup_changes(
    env: LocalSWEEnv,
    model,
    issue: str,
) -> None:
    """提交前让模型自检 diff，revert 掉与 issue 无关的文件。

    这是为了解决模型在探索过程中改了无关文件（如 pytest-5221 里多改了
    assertion/rewrite.py），导致 patch 不纯而失败的问题。
    """
    status = env.communicate("git status --short").strip()
    if not status:
        return
    diff_stat = env.communicate("git diff --stat HEAD").strip()
    if not diff_stat:
        return

    prompt = (
        "You are reviewing the changes made so far before submitting a fix.\n\n"
        "Issue description:\n"
        f"{issue}\n\n"
        "Files currently changed:\n"
        f"{diff_stat}\n\n"
        "Are there any files in the list above that are NOT necessary to fix the issue? "
        "For example: leftover experimental edits, test files that were modified unnecessarily, "
        "or changes to unrelated modules.\n\n"
        "Reply with the file paths that should be reverted, one per line. "
        "Use paths relative to the repo root (e.g. src/_pytest/assertion/rewrite.py). "
        "If all changes are necessary, reply with exactly the word 'none'."
    )
    try:
        response = model.query([{"role": "user", "content": prompt}])
    except Exception as exc:
        logger.warning("pre-submit review model query failed: %s", exc)
        return

    changed_output = env.communicate("git diff --name-only HEAD").strip()
    changed_files = {line for line in changed_output.splitlines() if line}
    files_to_revert = []
    for line in response.splitlines():
        line = line.strip().strip("`-*")
        if not line or line.lower() == "none":
            continue
        # Only accept an exact path reported by git. The final shell argument
        # is quoted below as repository file names may contain shell syntax.
        if line not in changed_files:
            continue
        files_to_revert.append(line)

    if not files_to_revert:
        logger.info("pre-submit review: no files to revert")
        return

    logger.info("pre-submit review reverting files: %s", files_to_revert)
    for path in files_to_revert:
        env.communicate("git restore --source=HEAD --staged --worktree -- " + shlex.quote(path))


def _filter_test_changes_from_patch(patch: str) -> str:
    """Drop any diff hunks for paths under testing/ or tests/.

    SWE-agent's submit.sh tries to revert test files, but staged and unstaged
    changes can still leak into model.patch. We sanitize the final patch here
    so evaluation only sees source-code changes.
    """
    if not patch.strip():
        return patch
    parts = re.split(r"^(diff --git )", patch, flags=re.MULTILINE)
    if len(parts) <= 1:
        return patch
    # parts[0] is preamble; parts[1], parts[3], ... are "diff --git " prefixes,
    # parts[2], parts[4], ... are the section bodies starting with "a/path b/path".
    result = [parts[0]]
    for prefix, body in zip(parts[1::2], parts[2::2]):
        first_line = body.splitlines()[0] if body else ""
        # first_line looks like "a/testing/test_assertion.py b/testing/test_assertion.py"
        match = re.match(r"a/(\S+)", first_line)
        path = match.group(1) if match else ""
        # 过滤任意层级的测试目录，不只顶层 testing/ / tests/。
        if (
            "/testing/" in path
            or "/tests/" in path
            or path.startswith("testing/")
            or path.startswith("tests/")
        ):
            continue
        result.append(prefix)
        result.append(body)
    return "".join(result)


def _materialize_agent_config(config_file: Path, output_dir: Path) -> Path:
    """Write a runtime config with command paths resolved beside the source config."""
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    command_files = []
    for value in data.get("command_files", []):
        path = Path(value)
        if not path.is_absolute():
            path = config_file.parent / path
        command_files.append(str(path.resolve()))
    data["command_files"] = command_files

    runtime_config = output_dir / "agent_config.yaml"
    runtime_config.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return runtime_config


def run_swe_agent_local(
    task: Any,
    workspace: Path,
    output_dir: Path,
    model_name: str = "deepseek-v4-flash",
    config_file: Path | None = None,
    max_steps: int = 100,
    timeout_per_command: int = 300,
) -> SWEAgentLocalResult:
    """Run SWE-agent locally and save patch for external evaluation."""
    start = time.monotonic()
    workspace = Path(workspace).resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if config_file is None:
        config_file = (
            Path(__file__).resolve().parent / "swe_agent_local_config" / "default_local.yaml"
        )
    config_file = _materialize_agent_config(Path(config_file).resolve(), output_dir)

    command_venv = output_dir / "command_venv"
    if command_venv.exists():
        shutil.rmtree(command_venv)
    subprocess.run(
        [sys.executable, "-m", "venv", str(command_venv)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    env = LocalSWEEnv(
        workspace,
        task,
        timeout=timeout_per_command,
        command_venv=command_venv,
    )
    env.reset()

    try:
        Agent, AgentArguments, ModelArguments = _load_sweagent_classes()
        model_args = ModelArguments(
            model_name=model_name,
            per_instance_cost_limit=10.0,
            total_cost_limit=100.0,
            temperature=0.0,
            top_p=0.95,
        )
        agent_args = AgentArguments(
            model=model_args,
            config_file=config_file,
            max_steps=max_steps,
        )
        agent = Agent("primary", agent_args)

        setup_args = {
            "issue": env.query,
        }

        traj_dir = output_dir / "traj"
        traj_dir.mkdir(parents=True, exist_ok=True)
        observation = env.communicate("ls")
        agent.run(
            setup_args=setup_args,
            env=env,
            observation=observation,
            traj_dir=traj_dir,
            return_type="info",
        )

        patch_path = workspace / "model.patch"
        patch = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""

        # 提交前让模型自检 diff，revert 掉探索过程中遗留的无关改动。
        _review_and_cleanup_changes(env, agent.model, env.query)

        # 清理后重新 submit，确保 model.patch 只保留真正相关的改动。
        logger.info("re-submitting after cleanup")
        env.communicate("submit")
        if patch_path.exists():
            patch = patch_path.read_text(encoding="utf-8")

        patch = _filter_test_changes_from_patch(patch)
        (output_dir / "agent.patch").write_text(patch, encoding="utf-8")

        duration = time.monotonic() - start
        if not patch.strip():
            return SWEAgentLocalResult(
                task_id=task.id,
                resolved=False,
                duration_seconds=duration,
                patch="",
                error="empty patch",
            )

        return SWEAgentLocalResult(
            task_id=task.id,
            resolved=False,
            duration_seconds=duration,
            patch=patch,
            error="pending external evaluation",
        )

    except Exception as exc:
        duration = time.monotonic() - start
        logger.exception("SWE-agent local failed for %s", task.id)
        return SWEAgentLocalResult(
            task_id=task.id,
            resolved=False,
            duration_seconds=duration,
            patch="",
            error=str(exc),
        )
    finally:
        env.close()


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from swe_bench.dataset import SWEBenchDataset

    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dataset", default="data/swe-bench-lite-test.json")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="Max agent steps (lower = faster but may not finish)",
    )
    parser.add_argument(
        "--timeout-per-command", type=int, default=300, help="Per-command bash timeout (s)"
    )
    args = parser.parse_args()

    ds = SWEBenchDataset(args.dataset)
    task = next(t for t in ds.list_tasks() if t.id == args.task_id)

    workspace = Path(args.workspace).resolve()
    output_dir = Path(args.output_dir).resolve()

    result = run_swe_agent_local(
        task,
        workspace,
        output_dir,
        model_name=args.model,
        max_steps=args.max_steps,
        timeout_per_command=args.timeout_per_command,
    )
    print(json.dumps(result.__dict__, indent=2, ensure_ascii=False))
