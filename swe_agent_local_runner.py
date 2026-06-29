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
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Add SWE-agent source to path
SWE_AGENT_ROOT = Path("/tmp/SWE-agent-0.7.0")
sys.path.insert(0, str(SWE_AGENT_ROOT))

from sweagent.agent.agents import Agent, AgentArguments
from sweagent.agent.models import ModelArguments

logger = logging.getLogger("swe_agent_local_runner")

LOCAL_CONFIG_DIR = Path(__file__).resolve().parent / "swe_agent_local_config"
BASH_BIN = "/Users/yihanwang/anaconda3/envs/swe_agent_py311/bin/bash"


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
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.task = task
        self.timeout = timeout
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
            "patch": getattr(task, "patch", "") or "",
            "FAIL_TO_PASS": getattr(task, "fail_to_pass", []) or [],
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
            'export SEARCH_RESULTS=()',
            'export SEARCH_FILES=()',
            'export SEARCH_INDEX=0',
            f'export WINDOW=${{WINDOW:-100}}',
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

    def _start_bash(self) -> None:
        """Start a persistent bash session and source the init script."""
        env = {**os.environ, "ROOT": str(self.workspace)}
        self._proc = subprocess.Popen(
            [BASH_BIN, "--norc", "--noprofile"],
            cwd=self.workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        # Source init script and echo a marker.
        marker = "__SWE_AGENT_INIT_DONE__"
        self._proc.stdin.write(f'source "{self._init_script}"\n')
        self._proc.stdin.write(f'echo "{marker}"\n')
        self._proc.stdin.flush()
        self._read_until(marker, timeout=30)

    def _read_until(self, marker: str, timeout: float = 120) -> str:
        """Read stdout until marker appears."""
        deadline = time.monotonic() + timeout
        buffer = []
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()  # type: ignore[union-attr]
            if marker in line:
                return "".join(buffer)
            buffer.append(line)
            if not line and self._proc.poll() is not None:
                break
        return "".join(buffer)

    def _send(self, cmd: str, timeout: float | None = None) -> str:
        """Send a command to the persistent bash and read output."""
        if self._proc is None or self._proc.poll() is not None:
            self._start_bash()
        timeout = timeout or self.timeout
        marker = f"__SWE_AGENT_DONE_{time.time()}__"
        # Escape marker in command output just in case.
        full_cmd = cmd.rstrip() + "\n" if not cmd.endswith("\n") else cmd
        # Append exit code marker.
        full_cmd += f'echo "{marker}"\necho "EXITSTATUS:$?"\n'
        self._proc.stdin.write(full_cmd)  # type: ignore[union-attr]
        self._proc.stdin.flush()  # type: ignore[union-attr]

        output = self._read_until(marker, timeout=timeout)
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
        self.communicate_output = output
        return output

    def communicate_with_handling(
        self, input_: str, error_msg: str = "", timeout_duration: int | None = None
    ) -> str:
        return self.communicate(input_, timeout_duration or self.timeout)

    def get_available_actions(self) -> list[str]:
        return []

    def step(self, action: str) -> tuple[str, float, bool, dict[str, Any]]:
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
        config_file = Path(__file__).resolve().parent / "swe_agent_local_config" / "default_local.yaml"

    env = LocalSWEEnv(workspace, task, timeout=timeout_per_command)
    env.reset()

    try:
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
        )
        agent = Agent("primary", agent_args)

        files = ""
        if task.patch:
            try:
                from unidiff import PatchSet

                files = "\n".join(f"- {x.path}" for x in PatchSet(task.patch).modified_files)
            except Exception:
                files = ""

        tests = "\n".join(f"- {x}" for x in (task.fail_to_pass or [])) or "none"

        setup_args = {
            "issue": env.query,
            "files": files,
            "tests": tests,
        }

        traj_dir = output_dir / "traj"
        traj_dir.mkdir(parents=True, exist_ok=True)
        observation = env.communicate("ls")
        info = agent.run(
            setup_args=setup_args,
            env=env,
            observation=observation,
            traj_dir=traj_dir,
            return_type="info",
        )

        patch_path = workspace / "model.patch"
        patch = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""
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
    args = parser.parse_args()

    ds = SWEBenchDataset(args.dataset)
    task = next(t for t in ds.list_tasks() if t.id == args.task_id)

    workspace = Path(args.workspace).resolve()
    output_dir = Path(args.output_dir).resolve()

    result = run_swe_agent_local(task, workspace, output_dir, model_name=args.model)
    print(json.dumps(result.__dict__, indent=2, ensure_ascii=False))
