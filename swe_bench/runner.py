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
from swe_bench.docker import DockerEvaluator
from swe_bench.environment import CondaEnvironmentBuilder
from swe_bench.evaluator import EvaluationResult, SWEBenchEvaluator
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
        timeout_seconds: float = 1200.0,
        mock_responses: str | Path | None = None,
        mode: str = "supervisor",
    ) -> None:
        self.config = config
        # Resolve to absolute paths: _prepare_workspace chdirs into cache_dir
        # during git clone, so a relative cache_dir would make the clone target
        # resolve against the new cwd and create a nested directory mess.
        self.output_dir = Path(output_dir).resolve()
        self.cache_dir = (
            Path(cache_dir).resolve()
            if cache_dir
            else (Path.home() / ".coding-agent" / "swe-bench-cache")
        )
        self.use_docker = use_docker
        self.max_workers = max_workers
        self.timeout_seconds = timeout_seconds
        self.mock_responses = Path(mock_responses) if mock_responses else None
        self.mode = mode  # "supervisor" (default) or "docker-bash"

        if self.max_workers != 1:
            raise SWEBenchRunnerError("M1 only supports sequential execution (max_workers=1)")

    def run_task(self, task: SWEBenchTask) -> TaskResult:
        """Run a single SWE-bench task end-to-end."""
        start = time.monotonic()
        task_output_dir = self.output_dir / task.id
        task_output_dir.mkdir(parents=True, exist_ok=True)
        workspace = task_output_dir / "workspace"

        if self.mode == "docker-bash":
            return self._run_task_docker_bash(task, task_output_dir, start)
        return self._run_task_supervisor(task, task_output_dir, workspace, start)

    def _run_task_docker_bash(
        self, task: SWEBenchTask, task_output_dir: Path, start: float
    ) -> TaskResult:
        """Run the agent directly inside a Docker container (bash-only mode).

        This mirrors mini-swe-agent's approach: the agent gets a single
        ``execute_shell`` tool that runs bash inside the official SWE-bench
        image. No supervisor/worker/IPC, no conda env — the container has the
        correct environment already.
        """
        import docker
        from swebench.harness.test_spec.test_spec import make_test_spec

        from agent.docker_bash_agent import DockerBashAgent, DockerShell
        from agent.llm.client import LLMClient
        from swe_bench.docker import (
            DOCKER_USER,
            DOCKER_WORKDIR,
            DockerEvaluationError,
            DockerEvaluator,
        )

        container = None
        evaluator = None
        try:
            client = docker.from_env()
            evaluator = DockerEvaluator(
                task, timeout_seconds=self.timeout_seconds, output_dir=self.output_dir
            )
            arch = evaluator._arch(client)
            spec = make_test_spec(task.to_instance_dict(), arch=arch, namespace="swebench")
            image = spec.instance_image_key
            logger.info("docker-bash: %s image=%s", task.id, image)

            try:
                evaluator._ensure_image(client, image)
                use_mount = False
            except DockerEvaluationError:
                logger.warning("official image unavailable; building locally")
                spec = make_test_spec(task.to_instance_dict(), arch=arch, namespace=None)
                evaluator._build_local_env_image(client, spec)
                image = spec.env_image_key
                use_mount = True

            # For local-build fallback we need a workspace to mount.
            workspace = task_output_dir / "workspace"
            if use_mount:
                self._prepare_workspace(task, workspace)

            create_kwargs: dict = {
                "image": image,
                "name": spec.get_instance_container_name(f"bash-{task.id}")[:63],
                "user": DOCKER_USER,
                "detach": True,
                "command": "tail -f /dev/null",
                "platform": spec.platform,
            }
            if use_mount:
                create_kwargs["volumes"] = {
                    str(workspace.resolve()): {"bind": DOCKER_WORKDIR, "mode": "rw"}
                }
            container = client.containers.create(**create_kwargs)
            container.start()
            logger.info("container %s started for %s", container.id[:12], task.id)

            evaluator.container = container
            evaluator._configure_container_pip(container)

            # Reset repo to base commit so the agent starts clean.
            shell = DockerShell(container=container, workdir=DOCKER_WORKDIR)
            shell.execute(f"git checkout -f {task.base_commit}", allow_destructive=True)
            shell.execute("git clean -fdx", allow_destructive=True)

            llm = LLMClient(self.config.llm)
            agent = DockerBashAgent(
                llm=llm,
                shell=shell,
                problem_statement=task.issue_title,
                step_limit=0,  # 0 = unlimited; controlled by wall_time_limit
                wall_time_limit=int(self.timeout_seconds),
            )
            patch = agent.run()

            patch_path = task_output_dir / "agent.patch"
            patch_path.write_text(patch, encoding="utf-8")

            if not patch.strip():
                return TaskResult(
                    task_id=task.id,
                    success=False,
                    resolved=False,
                    duration_seconds=time.monotonic() - start,
                    error="agent produced an empty patch",
                )

            # Evaluate in the same container (already at base_commit).
            eval_result = evaluator.evaluate(patch, workspace if use_mount else None)
            return TaskResult(
                task_id=task.id,
                success=eval_result.success,
                resolved=eval_result.resolved,
                duration_seconds=time.monotonic() - start,
                patch_path=str(patch_path),
                evaluation_stdout=eval_result.stdout,
                evaluation_stderr=eval_result.stderr,
                error=eval_result.error,
            )
        except Exception as exc:
            logger.exception("failed to run task %s (docker-bash)", task.id)
            return TaskResult(
                task_id=task.id,
                success=False,
                resolved=False,
                duration_seconds=time.monotonic() - start,
                error=str(exc),
            )
        finally:
            if container is not None:
                try:
                    container.stop(timeout=10)
                    container.remove(force=True)
                except Exception:  # noqa: BLE001
                    pass

    def _run_task_supervisor(
        self, task: SWEBenchTask, task_output_dir: Path, workspace: Path, start: float
    ) -> TaskResult:
        """Run via the full supervisor/worker/IPC pipeline (default mode)."""
        try:
            self._prepare_workspace(task, workspace)
            # Build a conda env matching the SWE-bench spec so the agent can run
            # the project's tests locally. This is best-effort: if the spec's
            # environment cannot be reproduced on the host (e.g. it pins Python
            # 3.9 but the package needs 3.10+), we fall back to the system
            # Python. The agent can still read/edit source and produce a patch,
            # and the final evaluation runs inside the official Docker image
            # which has the correct environment.
            env_name: str | None = None
            try:
                env_builder = CondaEnvironmentBuilder(
                    task, workspace, cache_dir=self.cache_dir / "envs"
                )
                env_name = env_builder.prepare(
                    timeout_seconds=max(1200.0, self.timeout_seconds * 2)
                )
            except Exception as env_exc:  # noqa: BLE001
                logger.warning(
                    "conda env setup failed for %s (falling back to system "
                    "python; docker eval will still use the official image): %s",
                    task.id,
                    env_exc,
                )
                env_name = None
            supervisor = self._start_supervisor(workspace, conda_env=env_name)
            timed_out = False
            try:
                timed_out = self._run_goal(supervisor, task)
                patch_path = task_output_dir / "agent.patch"
                PatchCollector.write_patch(workspace, patch_path)
                patch = patch_path.read_text(encoding="utf-8")
                if not patch.strip():
                    # Agent finished without producing changes. Treat this as a
                    # definitive failure (resolved=False) rather than crashing so
                    # the benchmark report stays complete.
                    return TaskResult(
                        task_id=task.id,
                        success=False,
                        resolved=False,
                        duration_seconds=time.monotonic() - start,
                        error="agent produced an empty patch",
                    )
                eval_result = self._evaluate(task, workspace, patch, conda_env=env_name)
                if timed_out:
                    # Preserve the evaluation result but flag the timeout.
                    eval_result.error = (
                        f"goal timed out after {self.timeout_seconds}s; "
                        f"{eval_result.error or ''}".strip()
                    )
            finally:
                supervisor.stop()

            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.id,
                success=eval_result.success and not timed_out,
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
        """Run all tasks sequentially and produce a report.

        If a task already has a ``report.json`` in its output directory, it is
        skipped so a previous run can be resumed safely.
        """
        from swe_bench.reporter import JSONReporter

        started_at = datetime.utcnow()
        results: list[TaskResult] = []
        for task in tasks:
            logger.info("running task %s (%d/%d)", task.id, len(results) + 1, len(tasks))
            task_output_dir = self.output_dir / task.id
            resume_path = task_output_dir / "report.json"
            if resume_path.exists():
                try:
                    previous = JSONReporter.load_task_result(resume_path)
                    logger.info("resuming task %s from %s", task.id, resume_path)
                    results.append(previous)
                    continue
                except Exception as exc:
                    logger.warning("failed to resume %s: %s", task.id, exc)
            result = self.run_task(task)
            JSONReporter.render_task_result(result, resume_path)
            results.append(result)
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
            self._ensure_repo_cache(task.repo, repo_cache)

        # Copy repo into workspace to avoid mutating the cache.
        if workspace.exists():
            shutil.rmtree(workspace)
        shutil.copytree(repo_cache, workspace)

        # The cache is a shallow clone (depth 1) to keep it small and fast on
        # flaky networks. The base_commit we need is usually not the tip, so
        # fetch it on demand before checking it out.
        self._fetch_commit(workspace, task.base_commit)
        _run_command(
            ["git", "checkout", "-f", task.base_commit],
            cwd=workspace,
            timeout=600,
        )
        _run_command(["git", "clean", "-fd"], cwd=workspace, timeout=300)

        logger.info("prepared workspace for %s at %s", task.id, workspace)

    def _fetch_commit(self, repo_dir: Path, commit: str) -> None:
        """Fetch a specific commit into a shallow clone (best-effort).

        GitHub supports fetching a single commit by SHA, which is tiny compared
        to a full clone. If the commit is already present (full cache or tip),
        this is a no-op.
        """
        try:
            _run_command(
                ["git", "fetch", "--depth", "1", "origin", commit],
                cwd=repo_dir,
                timeout=300,
            )
        except Exception as exc:  # noqa: BLE001
            # The commit may already be present (e.g. cache is not shallow), or
            # the server may not allow fetching arbitrary SHAs. Either way the
            # subsequent checkout will surface a real error if the commit truly
            # is missing.
            logger.debug("fetch of commit %s failed (may already be present): %s", commit, exc)

    def _ensure_repo_cache(self, repo: str, repo_cache: Path) -> None:
        """Ensure a clean shallow clone of ``repo`` exists at ``repo_cache``.

        Uses ``--depth 1`` so the initial clone is small and resilient to
        flaky networks (full clones of astropy/django are ~1GB and routinely
        fail with ``RPC failed; curl 18`` on restricted networks). The
        specific base_commit is fetched on demand per-task (see
        ``_fetch_commit``).
        """
        repo_cache.parent.mkdir(parents=True, exist_ok=True)

        def _is_valid_clone() -> bool:
            return repo_cache.exists() and (repo_cache / ".git").exists()

        if not _is_valid_clone():
            if repo_cache.exists():
                logger.warning("removing stale repo cache at %s", repo_cache)
                shutil.rmtree(repo_cache, ignore_errors=True)

            url = f"https://github.com/{repo}.git"
            last_err: Exception | None = None
            for attempt in range(1, 4):
                try:
                    logger.info("cloning %s (shallow, attempt %d/3)", url, attempt)
                    _run_command(
                        ["git", "clone", "--depth", "1", url, str(repo_cache)],
                        cwd=self.cache_dir,
                        timeout=600,
                    )
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    logger.warning("clone attempt %d failed: %s", attempt, exc)
                    shutil.rmtree(repo_cache, ignore_errors=True)
                    time.sleep(5 * attempt)
            if last_err is not None:
                raise SWEBenchRunnerError(f"failed to clone {repo} after 3 attempts: {last_err}")
        else:
            # Update the shallow tip so we have recent history.
            try:
                logger.info("fetching updates for %s", repo)
                _run_command(
                    ["git", "fetch", "--depth", "1", "origin"],
                    cwd=repo_cache,
                    timeout=600,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("fetch failed (continuing with cache): %s", exc)

    def _start_supervisor(self, workspace: Path, conda_env: str | None = None) -> Supervisor:
        """Start a Supervisor for the given workspace."""
        socket_address = f"/tmp/ca_swe_bench_{uuid.uuid4().hex[:8]}.sock"
        supervisor = Supervisor(
            workspace=str(workspace),
            config=self.config,
            socket_address=socket_address,
            conda_env=conda_env,
            confirm_callback=lambda _prompt: True,  # M1: auto-approve dangerous commands
        )
        if self.mock_responses is not None:
            supervisor._spawn_worker = self._make_mock_spawn_worker(self.mock_responses, workspace)
        supervisor.start()
        return supervisor

    def _run_goal(self, supervisor: Supervisor, task: SWEBenchTask) -> bool:
        """Submit a goal and wait for it to reach a terminal state.

        Returns ``True`` if the goal timed out (partial work may still exist in
        the workspace), ``False`` if it reached a terminal state normally.
        """
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
                return False
            time.sleep(0.5)

        supervisor.cancel_goal(goal.id)
        logger.warning("goal %s timed out after %ss", goal.id, self.timeout_seconds)
        return True

    def _build_goal_description(self, task: SWEBenchTask) -> str:
        """Build the goal description from the issue text."""
        parts: list[str] = []
        if task.issue_title:
            parts.append(f"Title: {task.issue_title}")
        if task.issue_body:
            parts.append(f"Description:\n{task.issue_body}")
        if task.hints_text:
            parts.append(f"Hints: {task.hints_text}")

        # SWE-bench specific workflow instructions to reduce agent exploration.
        fail_tests = ", ".join(task.fail_to_pass) if task.fail_to_pass else "<none specified>"
        pass_tests = ", ".join(task.pass_to_pass) if task.pass_to_pass else "<none specified>"
        instructions = (
            "You are fixing a real bug in an open-source repository. "
            "You MUST follow this workflow exactly:\n"
            "1. FIRST, run the failing tests to confirm you can reproduce the issue: "
            f"{fail_tests}. Report the failure. Do NOT skip this step.\n"
            "2. Read the relevant source files and explain the root cause in one sentence.\n"
            "3. Make the smallest possible code change that fixes the issue. "
            "Avoid adding new tests unless explicitly required.\n"
            f"4. Run the failing tests again ({fail_tests}) to confirm they pass.\n"
            f"5. Run the related existing tests ({pass_tests}) to ensure no regressions.\n"
            "6. If the tests do not pass, continue iterating.\n"
            "7. If you cannot fix the issue, explain why and do NOT return an empty patch.\n"
            "8. Do NOT commit any changes. Stop as soon as the tests pass."
        )
        parts.append(instructions)
        return "\n\n".join(parts)

    def _evaluate(
        self,
        task: SWEBenchTask,
        workspace: Path,
        patch: str,
        conda_env: str | None = None,
    ) -> EvaluationResult:
        """Run evaluation either locally in conda or inside the official Docker image."""
        if self.use_docker:
            return DockerEvaluator(
                task,
                timeout_seconds=self.timeout_seconds,
                output_dir=self.output_dir,
            ).evaluate(patch, workspace)

        evaluator = SWEBenchEvaluator(
            task,
            timeout_seconds=self.timeout_seconds,
            conda_env=conda_env,
        )
        return evaluator.evaluate(patch, workspace)

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
