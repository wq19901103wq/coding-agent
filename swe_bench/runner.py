"""Run SWE-bench tasks through the coding-agent Supervisor."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.config import Config
from agent.supervisor.models import GoalStatus
from agent.supervisor.supervisor import Supervisor
from swe_bench.dataset import SWEBenchTask
from swe_bench.environment import CondaEnvironmentBuilder
from swe_bench.evaluator import SWEBenchEvaluator
from swe_bench.patch_collector import PatchCollector
from swe_bench.reporter import BenchmarkMetadata, BenchmarkReport, TaskResult

logger = logging.getLogger("swe_bench.runner")


class SWEBenchRunnerError(Exception):
    """Raised when the runner encounters a fatal error."""


class SWEBenchRunner:
    """Orchestrate SWE-bench tasks using the coding-agent Supervisor."""

    def __init__(
        self,
        config: Config,
        output_dir: str | Path,
        cache_dir: str | Path | None = None,
        use_docker: bool = False,
        max_workers: int = 1,
        timeout_seconds: float = 600.0,
        mock_responses: str | Path | None = None,
    ) -> None:
        self.config = config
        self.output_dir = Path(output_dir)
        self.cache_dir = (
            Path(cache_dir) if cache_dir else Path.home() / ".coding-agent" / "swe-bench-cache"
        )
        self.use_docker = use_docker
        self.max_workers = max_workers
        self.timeout_seconds = timeout_seconds
        self.mock_responses = Path(mock_responses) if mock_responses else None

        if self.use_docker:
            raise SWEBenchRunnerError("Docker mode is not implemented in M1")
        if self.max_workers != 1:
            raise SWEBenchRunnerError("M1 only supports sequential execution (max_workers=1)")

    def run_task(self, task: SWEBenchTask) -> TaskResult:
        """Run a single SWE-bench task end-to-end."""
        start = time.monotonic()
        task_output_dir = self.output_dir / task.id
        task_output_dir.mkdir(parents=True, exist_ok=True)
        workspace = task_output_dir / "workspace"

        try:
            self._prepare_workspace(task, workspace)
            env_builder = CondaEnvironmentBuilder(
                task, workspace, cache_dir=self.cache_dir / "envs"
            )
            env_name = env_builder.prepare(timeout_seconds=max(1200.0, self.timeout_seconds * 2))
            supervisor = self._start_supervisor(workspace)
            try:
                self._run_goal(supervisor, task)
                patch_path = task_output_dir / "agent.patch"
                PatchCollector.write_patch(workspace, patch_path)
                patch = patch_path.read_text(encoding="utf-8")
                evaluator = SWEBenchEvaluator(
                    task,
                    timeout_seconds=self.timeout_seconds,
                    conda_env=env_name,
                )
                eval_result = evaluator.evaluate(patch, workspace)
            finally:
                supervisor.stop()

            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.id,
                success=eval_result.success,
                resolved=eval_result.resolved,
                duration_seconds=duration,
                patch_path=str(patch_path) if patch_path.exists() else None,
                evaluation_stdout=eval_result.stdout,
                evaluation_stderr=eval_result.stderr,
                error=eval_result.error,
            )
        except Exception as exc:
            logger.exception("failed to run task %s", task.id)
            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.id,
                success=False,
                resolved=False,
                duration_seconds=duration,
                error=str(exc),
            )

    def run_dataset(
        self,
        tasks: list[SWEBenchTask],
        dataset_path: str,
    ) -> BenchmarkReport:
        """Run all tasks sequentially and produce a report."""
        started_at = datetime.utcnow()
        results: list[TaskResult] = []
        for task in tasks:
            logger.info("running task %s (%d/%d)", task.id, len(results) + 1, len(tasks))
            results.append(self.run_task(task))
        finished_at = datetime.utcnow()

        return BenchmarkReport(
            metadata=BenchmarkMetadata(
                started_at=started_at,
                finished_at=finished_at,
                dataset_path=dataset_path,
                task_count=len(tasks),
                model=self.config.llm.model,
                provider=self.config.llm.provider,
            ),
            tasks=results,
        )

    def _prepare_workspace(self, task: SWEBenchTask, workspace: Path) -> None:
        """Clone or update the repo and check out the base commit.

        If ``task.repo`` is an absolute path or points to an existing local
        directory, it is used directly instead of cloning from GitHub.
        """
        repo_path = Path(task.repo)
        if repo_path.is_absolute() or repo_path.exists():
            repo_cache = repo_path.resolve()
        else:
            repo_cache = self.cache_dir / task.repo.replace("/", "__")
            if not repo_cache.exists():
                repo_cache.parent.mkdir(parents=True, exist_ok=True)
                _run_command(
                    ["git", "clone", f"https://github.com/{task.repo}.git", str(repo_cache)],
                    cwd=self.cache_dir,
                    timeout=300,
                )

        # Copy repo into workspace to avoid mutating the cache.
        if workspace.exists():
            shutil.rmtree(workspace)
        shutil.copytree(repo_cache, workspace)

        _run_command(
            ["git", "checkout", "-f", task.base_commit],
            cwd=workspace,
            timeout=60,
        )
        _run_command(["git", "clean", "-fd"], cwd=workspace, timeout=60)

        logger.info("prepared workspace for %s at %s", task.id, workspace)

    def _start_supervisor(self, workspace: Path) -> Supervisor:
        """Start a Supervisor for the given workspace."""
        socket_address = f"/tmp/ca_swe_bench_{uuid.uuid4().hex[:8]}.sock"
        supervisor = Supervisor(
            workspace=str(workspace),
            config=self.config,
            socket_address=socket_address,
            confirm_callback=lambda _prompt: True,  # M1: auto-approve dangerous commands
        )
        if self.mock_responses is not None:
            supervisor._spawn_worker = self._make_mock_spawn_worker(self.mock_responses, workspace)
        supervisor.start()
        return supervisor

    def _run_goal(self, supervisor: Supervisor, task: SWEBenchTask) -> None:
        """Submit a goal and wait for it to reach a terminal state."""
        description = self._build_goal_description(task)
        goal = supervisor.submit_goal(
            title=f"Fix {task.repo} issue {task.id}",
            description=description,
            agent_role="coder",
            timeout_seconds=self.timeout_seconds,
        )
        supervisor.run_goal(goal.id)

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            fetched = supervisor.persistence.get(goal.id)
            if fetched is None:
                raise SWEBenchRunnerError(f"goal {goal.id} disappeared")
            if fetched.status in (GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.CANCELLED):
                return
            time.sleep(0.5)

        supervisor.cancel_goal(goal.id)
        raise SWEBenchRunnerError(f"goal {goal.id} timed out after {self.timeout_seconds}s")

    def _build_goal_description(self, task: SWEBenchTask) -> str:
        """Build the goal description from the issue text."""
        parts: list[str] = []
        if task.issue_title:
            parts.append(task.issue_title)
        if task.issue_body:
            parts.append(task.issue_body)
        if task.hints_text:
            parts.append(f"Hints: {task.hints_text}")
        return "\n\n".join(parts)

    def _make_mock_spawn_worker(
        self,
        responses_path: Path,
        workspace: Path,
    ) -> Any:
        """Return a spawn_worker callable that injects mock LLM responses."""

        def spawn_worker(socket_address: str, goal: Any, cfg: Config) -> subprocess.Popen:
            cmd = [
                "python",
                "-m",
                "agent.worker.worker_main",
                "--socket",
                socket_address,
                "--workspace",
                str(workspace),
                "--role",
                goal.agent_role,
                "--mock-responses",
                str(responses_path),
            ]
            env = dict(os.environ)
            env["CODING_AGENT_LLM_API_KEY"] = cfg.llm.api_key or ""
            env["PYTHONUNBUFFERED"] = "1"

            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
            )
            config_json = cfg.model_dump_json()

            def _forward() -> None:
                if proc.stdout is None:
                    return
                for line in proc.stdout:
                    logger.debug("worker %s: %s", goal.id, line.rstrip())

            threading.Thread(target=_forward, daemon=True).start()

            if proc.stdin is not None:
                try:
                    proc.stdin.write(config_json)
                    proc.stdin.write("\n")
                    proc.stdin.close()
                except OSError:
                    logger.exception("failed to send config to mock worker")
            return proc

        return spawn_worker


def _run_command(cmd: list[str], cwd: Path, timeout: float) -> None:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise SWEBenchRunnerError(
            f"command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
