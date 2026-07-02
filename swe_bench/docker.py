"""Evaluate an agent-generated patch using SWE-bench Docker images."""

from __future__ import annotations

import logging
import platform as _platform
import subprocess
import traceback
import uuid
from pathlib import Path

import docker
import docker.errors
from swebench.harness.constants import (
    DOCKER_PATCH,
    DOCKER_USER,
    DOCKER_WORKDIR,
    ENV_IMAGE_BUILD_DIR,
    KEY_INSTANCE_ID,
    KEY_MODEL,
    KEY_PREDICTION,
)
from swebench.harness.docker_build import build_image
from swebench.harness.docker_utils import (
    cleanup_container,
    copy_to_container,
)
from swebench.harness.grading import get_eval_report
from swebench.harness.run_evaluation import GIT_APPLY_CMDS
from swebench.harness.test_spec.test_spec import make_test_spec

from swe_bench.dataset import SWEBenchTask
from swe_bench.evaluator import EvaluationResult

logger = logging.getLogger("swe_bench.docker")


class DockerEvaluationError(Exception):
    """Raised when Docker-based evaluation cannot be completed."""


class DockerEvaluator:
    """Evaluate a patch inside a SWE-bench instance container.

    The evaluator first tries to use the official pre-built images published by
    the SWE-bench project (``swebench/sweb.eval.x86_64.<instance_id>:latest``).
    When those cannot be pulled (e.g. behind a firewall), it falls back to
    building the environment and instance images locally.

    On Apple Silicon Macs the local build uses the ``linux/arm64`` platform so
    the container runs natively inside the Colima VM.

    Local-build fallback semantics
    ------------------------------
    When the official image is unavailable, the fallback mounts the *host*
    workspace into the container at ``/testbed`` (read-write) instead of
    letting the harness clone and install the repo inside the container.  This
    avoids cloning from GitHub inside the container, which is what we want in
    restricted networks, but it is a deliberate divergence from the official
    harness semantics:

    - the workspace is reset to ``base_commit`` and the agent patch is applied
      on top *inside the container* (see ``_clean_workspace`` + ``_apply_patch``);
    - file permissions, line endings and any host-side artefacts can in
      principle affect results, so treat fallback results as advisory unless
      reproducible with the official image.
    """

    def __init__(
        self,
        task: SWEBenchTask,
        timeout_seconds: float = 300.0,
        output_dir: str | Path | None = None,
        docker_base_url: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.task = task
        self.timeout_seconds = timeout_seconds
        self.output_dir = Path(output_dir) if output_dir else Path.cwd()
        self.docker_base_url = docker_base_url
        self.run_id = run_id or f"ca-{uuid.uuid4().hex[:8]}"

    def evaluate(self, patch: str, workspace: Path | None = None) -> EvaluationResult:
        """Apply ``patch`` and run the official test suite inside a Docker container."""
        patch = patch or ""
        task_output_dir = self.output_dir / self.task.id
        task_output_dir.mkdir(parents=True, exist_ok=True)

        # Clean the workspace back to the base commit before mounting it into the
        # container.  If the agent already modified files in the host workspace, the
        # mounted /testbed would already contain those changes; ``patch`` would then
        # detect a reversed patch and undo them, leaving the container with no fix.
        if workspace is not None:
            self._clean_workspace(Path(workspace))

        patch_file = task_output_dir / "agent.patch"
        patch_file.write_text(patch, encoding="utf-8")

        prediction = {
            KEY_INSTANCE_ID: self.task.id,
            KEY_MODEL: "coding-agent",
            KEY_PREDICTION: patch,
        }

        client = self._docker_client()
        arch = self._arch(client)
        spec = make_test_spec(
            self.task.to_instance_dict(),
            arch=arch,
            namespace="swebench",
        )
        image = spec.instance_image_key
        logger.info(
            "docker evaluating %s with image %s (platform=%s)",
            self.task.id,
            image,
            spec.platform,
        )

        container = None
        try:
            try:
                self._ensure_image(client, image)
            except DockerEvaluationError as exc:
                logger.warning(
                    "official image unavailable for %s (%s); building locally",
                    self.task.id,
                    exc,
                )
                spec = make_test_spec(
                    self.task.to_instance_dict(),
                    arch=arch,
                    namespace=None,
                )
                self._build_local_env_image(client, spec)
                image = spec.env_image_key
                use_workspace_mount = True
            else:
                use_workspace_mount = False

            create_kwargs: dict = {
                "image": image,
                "name": spec.get_instance_container_name(self.run_id),
                "user": DOCKER_USER,
                "detach": True,
                "command": "tail -f /dev/null",
                "platform": spec.platform,
                "cap_add": spec.docker_specs.get("run_args", {}).get("cap_add", []),
            }
            if use_workspace_mount:
                if workspace is None:
                    raise DockerEvaluationError(
                        "workspace is required for local Docker build fallback"
                    )
                workspace = Path(workspace).resolve()
                create_kwargs["volumes"] = {str(workspace): {"bind": DOCKER_WORKDIR, "mode": "rw"}}
            container = client.containers.create(**create_kwargs)
            container.start()
            logger.info("container %s started for %s", container.id[:12], self.task.id)

            # Configure pip inside the container to use a domestic mirror and retry,
            # reducing failures caused by intermittent PyPI access.
            self._configure_container_pip(container)

            copy_to_container(container, patch_file, Path(DOCKER_PATCH))
            if not self._apply_patch(container, patch):
                output = container.exec_run(
                    "git status", workdir=DOCKER_WORKDIR, user=DOCKER_USER
                ).output.decode("utf-8", errors="replace")
                return EvaluationResult(
                    success=False,
                    resolved=False,
                    stdout=output,
                    stderr="patch did not apply cleanly in container",
                    exit_code=1,
                    error="patch did not apply cleanly in container",
                )

            eval_file = task_output_dir / "eval.sh"
            eval_file.write_text(spec.eval_script, encoding="utf-8")
            copy_to_container(container, eval_file, Path("/eval.sh"))

            # Run the eval script directly instead of swebench's
            # exec_run_with_timeout: that helper calls .decode() with no error
            # handling and crashes on non-UTF-8 bytes (common in C-extension
            # test output from astropy/matplotlib).
            test_output, timed_out = self._run_eval_script(container, int(self.timeout_seconds))
            test_output_path = task_output_dir / "test_output.txt"
            test_output_path.write_text(test_output, encoding="utf-8", errors="replace")

            if timed_out:
                return EvaluationResult(
                    success=False,
                    resolved=False,
                    stdout=test_output,
                    stderr=f"timed out after {self.timeout_seconds}s",
                    exit_code=None,
                    error=f"docker evaluation timed out after {self.timeout_seconds}s",
                )

            report = get_eval_report(
                test_spec=spec,
                prediction=prediction,
                test_log_path=str(test_output_path),
                include_tests_status=True,
            )
            task_report = report.get(self.task.id, {})
            resolved = task_report.get("resolved", False)
            tests_status = task_report.get("tests_status", {})

            return EvaluationResult(
                success=True,
                resolved=resolved,
                stdout=test_output,
                stderr=str(tests_status) if tests_status else "",
                exit_code=0 if resolved else 1,
                error=None,
            )
        except Exception as exc:
            logger.exception("docker evaluation failed for %s", self.task.id)
            return EvaluationResult(
                success=False,
                resolved=False,
                stdout="",
                stderr=traceback.format_exc(),
                exit_code=None,
                error=f"docker evaluation failed: {exc}",
            )
        finally:
            if container is not None:
                cleanup_container(client, container, logger)

    def evaluate_in_container(self, container, spec) -> EvaluationResult:
        """Evaluate using an *already-running* container (docker-bash mode).

        The agent has already applied its changes in the container, so we skip
        image setup / create / start / apply-patch and just run the eval
        script. This avoids a second container lifecycle (and the docker
        connection errors it can trigger on an unstable daemon).
        """
        task_output_dir = self.output_dir / self.task.id
        task_output_dir.mkdir(parents=True, exist_ok=True)

        eval_file = task_output_dir / "eval.sh"
        eval_file.write_text(spec.eval_script, encoding="utf-8")
        copy_to_container(container, eval_file, Path("/eval.sh"))

        test_output, timed_out = self._run_eval_script(container, int(self.timeout_seconds))
        test_output_path = task_output_dir / "test_output.txt"
        test_output_path.write_text(test_output, encoding="utf-8", errors="replace")

        if timed_out:
            return EvaluationResult(
                success=False,
                resolved=False,
                stdout=test_output,
                stderr=f"timed out after {self.timeout_seconds}s",
                exit_code=None,
                error=f"docker evaluation timed out after {self.timeout_seconds}s",
            )

        prediction = {
            KEY_INSTANCE_ID: self.task.id,
            KEY_MODEL: "docker-bash",
            KEY_PREDICTION: "",
        }
        report = get_eval_report(
            test_spec=spec,
            prediction=prediction,
            test_log_path=str(test_output_path),
            include_tests_status=True,
        )
        task_report = report.get(self.task.id, {})
        resolved = task_report.get("resolved", False)
        tests_status = task_report.get("tests_status", {})
        return EvaluationResult(
            success=True,
            resolved=resolved,
            stdout=test_output,
            stderr=str(tests_status) if tests_status else "",
            exit_code=0 if resolved else 1,
            error=None,
        )

    def _configure_container_pip(self, container) -> None:
        """Configure pip inside the container to use a configurable mirror.

        The index URL mirrors the host setting via SWE_BENCH_PIP_INDEX_URL
        (defaulting to the Tsinghua mirror) so restricted-network runs keep
        working while CI/overseas runs can point at the official PyPI.
        """
        import os

        pip_index_url = os.environ.get(
            "SWE_BENCH_PIP_INDEX_URL",
            "https://pypi.tuna.tsinghua.edu.cn/simple",
        )
        commands = [
            f"python -m pip config set global.index-url {pip_index_url}",
            "python -m pip config set global.timeout 120",
            "python -m pip config set global.retries 5",
        ]
        for cmd in commands:
            result = container.exec_run(
                cmd,
                workdir=DOCKER_WORKDIR,
                user=DOCKER_USER,
            )
            output = result.output.decode("utf-8", errors="replace")
            if result.exit_code != 0:
                logger.warning("failed to configure container pip: %s", output)
            else:
                logger.debug("configured container pip: %s", output.strip())

    def _clean_workspace(self, workspace: Path) -> None:
        """Reset ``workspace`` to the task's base commit so patch applies cleanly."""
        workspace = Path(workspace).resolve()
        if not (workspace / ".git").exists():
            logger.warning("workspace %s is not a git repo; skipping clean", workspace)
            return
        try:
            subprocess.run(
                ["git", "-C", str(workspace), "checkout", "-f", self.task.base_commit],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(workspace), "clean", "-fd"],
                check=True,
                capture_output=True,
                text=True,
            )
            # Strip host-side bytecode caches so the mounted workspace does not
            # reference host paths (e.g. /Users/...) inside the container.
            subprocess.run(
                [
                    "find",
                    str(workspace),
                    "-type",
                    "d",
                    "-name",
                    "__pycache__",
                    "-exec",
                    "rm",
                    "-rf",
                    "{}",
                    "+",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["find", str(workspace), "-name", "*.pyc", "-delete"],
                check=False,
                capture_output=True,
                text=True,
            )
            logger.info("cleaned workspace %s to base commit %s", workspace, self.task.base_commit)
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "failed to clean workspace %s: %s\nstdout: %s\nstderr: %s",
                workspace,
                exc,
                exc.stdout,
                exc.stderr,
            )

    def _arch(self, client: docker.DockerClient) -> str:
        """Return the SWE-bench architecture name for the Docker daemon."""
        try:
            daemon_arch = client.version().get("Arch", "").lower()
        except Exception:
            daemon_arch = ""
        if daemon_arch in ("arm64", "aarch64"):
            return "arm64"
        if daemon_arch == "amd64":
            return "x86_64"
        # Fallback to the host Python architecture.
        machine = _platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            return "arm64"
        return "x86_64"

    def _docker_client(self) -> docker.DockerClient:
        """Create a Docker client, falling back to the Colima socket on macOS."""
        if self.docker_base_url:
            return docker.DockerClient(base_url=self.docker_base_url)

        try:
            return docker.from_env()
        except docker.errors.DockerException:
            colima_sock = Path.home() / ".colima" / "default" / "docker.sock"
            if colima_sock.exists():
                return docker.DockerClient(base_url=f"unix://{colima_sock}")
            raise

    def _ensure_image(self, client: docker.DockerClient, image: str) -> None:
        """Pull the instance image if it is not already present locally."""
        try:
            client.images.get(image)
            logger.info("using local docker image %s", image)
            return
        except docker.errors.ImageNotFound:
            pass

        # Docker Hub is often unreachable; fail fast (5 s) instead of hanging.
        logger.info("pulling docker image %s (timeout 5s)", image)
        try:
            # docker SDK 7.x removed the timeout kwarg from APIClient.pull;
            # use a short socket timeout to avoid hanging forever.

            original_timeout = client.api.timeout
            client.api.timeout = 5
            try:
                client.api.pull(image, stream=False)
            finally:
                client.api.timeout = original_timeout
        except docker.errors.NotFound as exc:
            raise DockerEvaluationError(f"docker image not found: {image}") from exc
        except Exception as exc:
            raise DockerEvaluationError(f"failed to pull docker image {image}: {exc}") from exc

    def _build_local_env_image(self, client, spec) -> None:
        """Build the environment image locally for the given spec.

        The instance image is not built; instead the host workspace is mounted
        into the container at ``/testbed``.  This avoids cloning from GitHub
        inside the container, which often fails in restricted networks.
        """
        # Base image must already exist (it is too expensive to rebuild here and
        # needs network-specific configuration such as conda mirrors).
        try:
            client.images.get(spec.base_image_key)
            logger.info("using local base image %s", spec.base_image_key)
        except docker.errors.ImageNotFound as exc:
            raise DockerEvaluationError(
                f"base image {spec.base_image_key} not found; "
                "build it first (see SWE_BENCH_DOCKER_SETUP.md)"
            ) from exc

        env_name = spec.env_image_key
        env_build_dir = ENV_IMAGE_BUILD_DIR / env_name.replace(":", "__")
        try:
            client.images.get(env_name)
            logger.info("using local env image %s", env_name)
        except docker.errors.ImageNotFound:
            logger.info("building local env image %s", env_name)
            build_image(
                image_name=env_name,
                setup_scripts={"setup_env.sh": spec.setup_env_script},
                dockerfile=spec.env_dockerfile,
                platform=spec.platform,
                client=client,
                build_dir=env_build_dir,
                nocache=False,
            )

    def _apply_patch(self, container, patch: str) -> bool:
        """Try to apply the agent patch inside the running container."""
        if not patch.strip():
            logger.info("empty patch, nothing to apply")
            return True

        for cmd in GIT_APPLY_CMDS:
            val = container.exec_run(
                f"{cmd} {DOCKER_PATCH}",
                workdir=DOCKER_WORKDIR,
                user=DOCKER_USER,
            )
            output = val.output.decode("utf-8", errors="replace")
            if val.exit_code == 0:
                logger.info("patch applied with '%s'", cmd)
                logger.info("patch apply output:\n%s", output)
                # Verify the patch actually changed files.
                verify = container.exec_run(
                    "git diff --stat",
                    workdir=DOCKER_WORKDIR,
                    user=DOCKER_USER,
                )
                verify_output = verify.output.decode("utf-8", errors="replace")
                logger.info("post-patch git diff stat:\n%s", verify_output)
                return True
            logger.debug("patch apply attempt failed with '%s':\n%s", cmd, output)

        logger.error("all patch apply attempts failed for %s", self.task.id)
        return False

    def _run_eval_script(self, container, timeout: int) -> tuple[str, bool]:
        """Run /eval.sh in the container, returning (output, timed_out).

        Replaces swebench's ``exec_run_with_timeout``, which crashes on
        non-UTF-8 bytes in test output. We decode with ``errors="replace"``
        so C-extension garble doesn't abort evaluation.
        """
        import threading

        result: dict = {"output": b"", "timed_out": False, "done": False}

        def _run() -> None:
            try:
                res = container.exec_run(
                    "/bin/bash /eval.sh",
                    workdir=DOCKER_WORKDIR,
                    user=DOCKER_USER,
                    demux=False,
                )
                raw = res.output
                if isinstance(raw, tuple):
                    # demux=False still returns (stdout, stderr) in some versions
                    raw = b"".join(p or b"" for p in raw)
                result["output"] = raw or b""
            except Exception as exc:  # noqa: BLE001
                logger.warning("eval exec failed: %s", exc)
                result["output"] = str(exc).encode("utf-8", errors="replace")
            finally:
                result["done"] = True

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            result["timed_out"] = True
            logger.warning("eval script timed out after %ss", timeout)

        output = result["output"]
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return output, result["timed_out"]
