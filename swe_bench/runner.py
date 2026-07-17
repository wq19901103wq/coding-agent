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


def _new_docker_client():
    """Create a fresh Docker client.

    A new client per operation is the simplest way to avoid stale-connection
    errors when Colima's daemon drops the TCP socket during long batch runs.
    """
    import docker

    return docker.from_env()


def _ensure_bare_image(client, image: str) -> None:
    """Ensure *image* exists locally, pulling it if necessary.

    Used for the ``python:3.10-slim`` fallback image, which may not be cached
    in Colima. The pull is retried since registry access can be flaky.
    """
    import time as _time

    for attempt in range(3):
        try:
            client.images.get(image)
            return  # already present
        except Exception:  # noqa: BLE001
            try:
                logger.info("pulling bare image %s (attempt %d/3)", image, attempt + 1)
                client.images.pull(image)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("pull %s attempt %d failed: %s", image, attempt + 1, exc)
                _time.sleep(5 * (attempt + 1))
    # If we get here, the pull failed 3 times. The create will fail with a
    # clear ImageNotFound error, which is handled by the caller.


def _restart_docker_daemon() -> None:
    """Restart the Docker daemon (Colima) to clear stale connections.

    Colima accumulates connection leaks over long batch runs, eventually
    causing RemoteDisconnected errors. Stopping and starting Colima
    recreates the daemon socket with a clean state. Safe to call even if
    Colima isn't the runtime (falls back to a no-op).
    """
    import shutil as _shutil
    import time as _time

    if not _shutil.which("colima"):
        logger.debug("colima not found; skipping daemon restart")
        return

    logger.info("restarting Colima daemon to clear stale connections...")
    try:
        subprocess.run(["colima", "stop", "--force"], capture_output=True, timeout=60)
        _time.sleep(3)
        subprocess.run(["colima", "start"], capture_output=True, timeout=120)
        _time.sleep(5)
        # Verify the daemon is back up.
        import docker as _docker

        client = _docker.from_env()
        client.ping()
        logger.info("Colima restarted successfully; daemon is responsive")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Colima restart failed (continuing anyway): %s", exc)


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
        docker_restart_interval: int = 6,
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
        # Restart the Docker daemon (Colima) every N tasks to prevent the
        # connection leaks that cause RemoteDisconnected after long runs.
        self.docker_restart_interval = docker_restart_interval

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
        if self.mode == "direct":
            return self._run_task_direct(task, task_output_dir, workspace, start)
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
            # Create a fresh docker client per task: Colima's daemon can drop
            # connections during long batch runs, and a stale client causes
            # RemoteDisconnected errors. A fresh client per task sidesteps this.
            client = _new_docker_client()
            evaluator = DockerEvaluator(
                task, timeout_seconds=self.timeout_seconds, output_dir=self.output_dir
            )
            arch = evaluator._arch(client)
            spec = make_test_spec(task.to_instance_dict(), arch=arch, namespace="swebench")
            image = spec.instance_image_key
            logger.info("docker-bash: %s image=%s", task.id, image)

            use_mount = False
            try:
                evaluator._ensure_image(client, image)
            except DockerEvaluationError:
                logger.warning("official image unavailable; trying local build")
                try:
                    spec = make_test_spec(task.to_instance_dict(), arch=arch, namespace=None)
                    evaluator._build_local_env_image(client, spec)
                    image = spec.env_image_key
                    use_mount = True
                except Exception as build_err:  # noqa: BLE001
                    # Last-resort fallback: a bare Python image with the
                    # workspace mounted. The agent can still read/edit source
                    # and produce a patch, even if it can't run the project's
                    # tests (missing compiled deps). This is far better than
                    # failing the task outright with an empty patch.
                    logger.warning(
                        "local env build failed for %s (%s); falling back to bare python image",
                        task.id,
                        build_err,
                    )
                    image = "python:3.10-slim"
                    use_mount = True
                    # Re-make spec without namespace for container naming.
                    spec = make_test_spec(task.to_instance_dict(), arch=arch, namespace=None)
                    # Ensure the bare image is present locally (pull if missing).
                    # The pull itself can fail on flaky networks, so retry.
                    _ensure_bare_image(client, image)

            # For local-build fallback we need a workspace to mount.
            workspace = task_output_dir / "workspace"
            if use_mount:
                self._prepare_workspace(task, workspace)

            is_bare_image = image == "python:3.10-slim"
            create_kwargs: dict = {
                "image": image,
                "name": spec.get_instance_container_name(f"bash-{task.id}")[:63],
                # Bare python images don't have the swebench user; use root.
                "user": "root" if is_bare_image else DOCKER_USER,
                "detach": True,
                "command": "tail -f /dev/null",
                "platform": spec.platform,
            }
            if use_mount:
                create_kwargs["volumes"] = {
                    str(workspace.resolve()): {"bind": DOCKER_WORKDIR, "mode": "rw"}
                }
            # Create + start with retry: Colima's daemon connection can drop
            # mid-batch, manifesting as RemoteDisconnected on container ops.
            container = None
            for attempt in range(3):
                try:
                    client = _new_docker_client()
                    container = client.containers.create(**create_kwargs)
                    container.start()
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("container create/start attempt %d failed: %s", attempt + 1, exc)
                    if attempt < 2:
                        time.sleep(5 * (attempt + 1))
                    else:
                        raise
            assert container is not None
            logger.info("container %s started for %s", container.id[:12], task.id)

            if is_bare_image:
                # Bare python:3.10-slim lacks git and build tools; install them.
                shell_tmp = DockerShell(
                    client=client, container_id=container.id, workdir=DOCKER_WORKDIR
                )
                shell_tmp.execute(
                    "apt-get update -qq && apt-get install -y -qq git > /dev/null 2>&1",
                    allow_destructive=True,
                )
            else:
                evaluator._configure_container_pip(container)

            # Reset repo to base commit so the agent starts clean.
            shell = DockerShell(client=client, container_id=container.id, workdir=DOCKER_WORKDIR)
            # Fetch the base_commit if missing (shallow cache), then checkout.
            shell.execute(
                f"git fetch --depth 1 origin {task.base_commit} 2>/dev/null; "
                f"git checkout -f {task.base_commit}",
                allow_destructive=True,
            )
            shell.execute("git clean -fdx", allow_destructive=True)

            llm = LLMClient(self.config.llm)
            agent = DockerBashAgent(
                llm=llm,
                shell=shell,
                problem_statement=task.issue_title,
                step_limit=0,  # 0 = unlimited; controlled by wall_time_limit
                wall_time_limit=int(self.timeout_seconds),
                pass_to_pass=task.pass_to_pass,
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

            # Evaluate in the *same* container the agent used (already has
            # the agent's changes applied). This avoids a second container
            # lifecycle which is the main source of docker connection errors.
            eval_result = evaluator.evaluate_in_container(container, spec)
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

    def _run_task_direct(
        self, task: SWEBenchTask, task_output_dir: Path, workspace: Path, start: float
    ) -> TaskResult:
        """Run the agent directly in-process with zero IPC overhead.

        No supervisor, no worker subprocess, no IPC serialization.  The LLM
        calls tools directly inside the runner process — same architecture as
        Claude Code's agent loop.
        """
        from agent.direct_agent import DirectAgent
        from agent.llm.client import LLMClient
        from agent.supervisor.role_loader import RoleLoader

        try:
            self._prepare_workspace(task, workspace)

            # Build conda env (best-effort, same as supervisor mode)
            env_name: str | None = None
            try:
                from swe_bench.environment import CondaEnvironmentBuilder

                env_builder = CondaEnvironmentBuilder(
                    task, workspace, cache_dir=self.cache_dir / "envs"
                )
                env_name = env_builder.prepare(
                    timeout_seconds=max(1200.0, self.timeout_seconds * 2)
                )
            except Exception as env_exc:  # noqa: BLE001
                logger.warning(
                    "conda env setup failed for %s (falling back to system python): %s",
                    task.id,
                    env_exc,
                )
                env_name = None

            # Load coder role for system prompt
            loader = RoleLoader()
            coder_role = loader.get("coder")

            # Build goal description (same as supervisor mode)
            description = self._build_goal_description(task)

            llm = LLMClient(self.config.llm)
            agent = DirectAgent(
                llm=llm,
                workspace=workspace,
                system_prompt=coder_role.system_prompt,
                allowed_tools=coder_role.allowed_tools,
                log_path=task_output_dir / "agent.log",
                conda_env=env_name,
                allow_dangerous_shell=True,
            )

            # The trusted benchmark runner grants shell consent explicitly.
            # Normal users cannot enable this path with an environment variable.
            agent_answer = agent.run(
                goal_description=description,
                max_steps=self.config.llm.max_steps_per_turn,
            )

            # Collect patch
            patch_path = task_output_dir / "agent.patch"
            PatchCollector.write_patch(workspace, patch_path)
            patch = patch_path.read_text(encoding="utf-8")
            if not patch.strip():
                return TaskResult(
                    task_id=task.id,
                    success=False,
                    resolved=False,
                    duration_seconds=time.monotonic() - start,
                    error=(
                        agent_answer
                        if agent_answer.startswith(("LLM error", "Reached token budget"))
                        else "agent produced an empty patch"
                    ),
                )

            # Evaluate
            eval_result = self._evaluate(task, workspace, patch, conda_env=env_name)
            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.id,
                success=True,
                resolved=eval_result.resolved,
                duration_seconds=duration,
                error=eval_result.error if not eval_result.resolved else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to run task %s (direct)", task.id)
            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.id,
                success=False,
                resolved=False,
                duration_seconds=duration,
                error=str(exc),
            )

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
            # Periodically restart the Docker daemon (Colima) to prevent the
            # connection leaks that cause RemoteDisconnected errors after
            # ~2-3 hours of continuous use. Only applies to docker modes.
            if self.mode == "docker-bash" and self.use_docker:
                # Count tasks actually run (not resumed) since last restart.
                # Use the task index in the full list for simplicity.
                task_index = tasks.index(task)
                if task_index > 0 and task_index % self.docker_restart_interval == 0:
                    _restart_docker_daemon()
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

        # Local Docker image builds may derive package versions from Git tags.
        # Ensure the base commit has enough ancestry before copying the cache;
        # a depth-1 commit otherwise becomes versions such as ``0.1.dev1``.
        self._fetch_commit(repo_cache, task.base_commit)

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
        """Fetch a specific commit into a shallow clone with retries.

        Tries origin first (with retries), then falls back to a gitee mirror.
        GitHub can be unreliable from inside containers in restricted-network
        environments, so we retry each source with increasing timeouts.
        """
        import random as _random
        import re

        # A present commit is not sufficient: depth-1 history prevents tools
        # such as setuptools-scm from finding the nearest release tag.
        commit_exists = False
        try:
            _run_command(
                ["git", "cat-file", "-t", commit],
                cwd=repo_dir,
                timeout=5,
            )
            commit_exists = True
        except Exception:
            pass
        if commit_exists:
            try:
                shallow = (
                    _run_command(
                        ["git", "rev-parse", "--is-shallow-repository"],
                        cwd=repo_dir,
                        timeout=5,
                    ).stdout.strip()
                    == "true"
                )
                history_count = int(
                    _run_command(
                        ["git", "rev-list", "--count", commit],
                        cwd=repo_dir,
                        timeout=10,
                    ).stdout.strip()
                )
                if not shallow or history_count >= 200:
                    return
            except Exception:
                pass

        # Build source list: origin → gitee mirror (if URL pattern matches).
        sources: list[str] = ["origin"]
        try:
            remote_url = _run_command(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_dir,
                timeout=5,
            ).stdout.strip()
            m = re.search(r"github\.com/(.+?)(?:\.git)?$", remote_url)
            if m:
                sources.append(self._mirror_url(m.group(1)))
        except Exception:
            pass

        for source in sources:
            for attempt in range(1, 4):
                try:
                    timeout = 30 * attempt  # 30s, 60s, 90s
                    _run_command(
                        ["git", "fetch", "--depth", "500", "--tags", source, commit],
                        cwd=repo_dir,
                        timeout=timeout,
                    )
                    logger.info(
                        "fetched commit %s from %s (attempt %d)",
                        commit[:8],
                        source,
                        attempt,
                    )
                    return
                except Exception as exc:
                    logger.debug(
                        "fetch commit %s from %s attempt %d failed: %s",
                        commit[:8],
                        source,
                        attempt,
                        exc,
                    )
                    wait = min(5 * attempt + _random.random(), 30)
                    time.sleep(wait)

        # The commit may already be present (e.g. cache is not shallow), or
        # the server may not allow fetching arbitrary SHAs. Either way the
        # subsequent checkout will surface a real error if the commit truly
        # is missing.
        logger.debug(
            "could not fetch commit %s from any source; "
            "it may already be present in the shallow cache",
            commit[:8],
        )

    # Mirror URL for repos when github.com is unreachable.
    # gitee.com/mirrors hosts many popular GitHub repos and supports
    # fetching arbitrary SHAs (unlike many other mirrors).
    _GIT_MIRROR_BASE = "https://gitee.com/mirrors"

    @staticmethod
    def _mirror_url(repo: str) -> str:
        """Return a gitee mirror URL for the given GitHub repo.

        e.g. ``django/django`` → ``https://gitee.com/mirrors/django.git``
        """
        repo_name = repo.split("/")[-1]
        return f"{SWEBenchRunner._GIT_MIRROR_BASE}/{repo_name}.git"

    def _ensure_repo_cache(self, repo: str, repo_cache: Path) -> None:
        """Ensure a clone of ``repo`` exists at ``repo_cache``.

        Uses a deeper shallow clone (``--depth 500``) so that most SWE-bench
        base_commits are already present without needing to fetch individual
        SHAs from the network. When GitHub is unreachable, falls back to a
        mirror (gitclone.com).
        """
        repo_cache.parent.mkdir(parents=True, exist_ok=True)

        def _is_valid_clone() -> bool:
            return repo_cache.exists() and (repo_cache / ".git").exists()

        if not _is_valid_clone():
            if repo_cache.exists():
                logger.warning("removing stale repo cache at %s", repo_cache)
                shutil.rmtree(repo_cache, ignore_errors=True)

            urls = [
                f"https://github.com/{repo}.git",
                self._mirror_url(repo),
            ]
            for url in urls:
                last_err: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        logger.info("cloning %s (depth 500, attempt %d/3)", url, attempt)
                        _run_command(
                            ["git", "clone", "--depth", "500", url, str(repo_cache)],
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
                if last_err is None:
                    break  # clone succeeded with this URL
            if last_err is not None:
                raise SWEBenchRunnerError(f"failed to clone {repo} after all attempts: {last_err}")
        else:
            # Update the shallow tip so we have recent history.
            try:
                logger.info("fetching updates for %s", repo)
                _run_command(
                    ["git", "fetch", "--depth", "1", "origin"],
                    cwd=repo_cache,
                    timeout=60,
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
        # SWE-bench specific workflow: focus the agent on finding and fixing the
        # bug with minimal steps.  The previous "run tests first" strategy wasted
        # ~30% of turns on environment issues (especially when conda is broken).
        #
        # 合规说明：不向 agent 泄露 FAIL_TO_PASS 测试名。SWE-bench 标准评估里
        # agent 只应拿到 issue 描述（problem statement），验收测试由评估 harness
        # 在 agent 不可见的情况下运行。泄露测试名等于给出验收标准，属于作弊。
        instructions = (
            "You are fixing a real bug in this repository.\n\n"
            "## Bug\n"
            "Use the problem statement above to understand the required behavior "
            "and fix the source code accordingly. The correctness of your fix is "
            "verified by a hidden test suite run by the evaluation harness — you "
            "cannot see those tests, so reason carefully about the expected "
            "behavior described in the problem statement.\n\n"
            "## Workflow (do this efficiently)\n"
            "1. Read the problem statement above. Understand what the bug is.\n"
            "2. Find the relevant source files using code_search or glob_search.\n"
            "3. Read the source code carefully and identify the root cause.\n"
            "4. Apply the smallest possible fix using str_replace_file.\n"
            "5. Sanity-check your fix against the problem statement and, if useful, "
            "any existing tests already present in the repo. Do not attempt to "
            "guess or recreate the hidden verification tests.\n\n"
            "## Rules\n"
            "- NEVER modify pyproject.toml, setup.cfg, setup.py, tox.ini, Makefile, "
            "or any config file.\n"
            "- NEVER modify or add test files under testing/ or tests/.\n"
            "- NEVER run pip install, conda install, or any package manager.\n"
            "- NEVER debug the environment — if a command fails, try a different approach.\n"
            "- Make the MINIMAL change — edit only the few lines that cause the bug.\n"
            "- Do NOT commit, create a branch, or use git stash/reset/revert.\n"
            "- Work in the current directory; do NOT cd to /home/user or other paths.\n"
            "- Reason about correctness before reporting completion; do not skip verification."
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


def _run_command(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
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
    return result
