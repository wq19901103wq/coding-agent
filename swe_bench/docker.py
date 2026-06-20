"""Evaluate an agent-generated patch using the official SWE-bench Docker images."""

from __future__ import annotations

import logging
import traceback
import uuid
from pathlib import Path

import docker
import docker.errors
from swebench.harness.constants import (
    DOCKER_PATCH,
    DOCKER_USER,
    DOCKER_WORKDIR,
    KEY_INSTANCE_ID,
    KEY_MODEL,
    KEY_PREDICTION,
)
from swebench.harness.docker_utils import (
    cleanup_container,
    copy_to_container,
    exec_run_with_timeout,
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
    """Evaluate a patch inside the official SWE-bench instance container.

    This uses the pre-built images published by the SWE-bench project
    (``swebench/sweb.eval.x86_64.<instance_id>:latest`` by default) so that
    evaluation happens in the exact same Linux environment used by the
    official harness, avoiding macOS-specific build problems.
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

        patch_file = task_output_dir / "agent.patch"
        patch_file.write_text(patch, encoding="utf-8")

        prediction = {
            KEY_INSTANCE_ID: self.task.id,
            KEY_MODEL: "coding-agent",
            KEY_PREDICTION: patch,
        }

        client = self._docker_client()
        spec = make_test_spec(self.task.to_instance_dict())
        image = spec.instance_image_key
        logger.info(
            "docker evaluating %s with image %s (platform=%s)",
            self.task.id,
            image,
            spec.platform,
        )

        container = None
        try:
            self._ensure_image(client, image)
            container = client.containers.create(
                image=image,
                name=spec.get_instance_container_name(self.run_id),
                user=DOCKER_USER,
                detach=True,
                command="tail -f /dev/null",
                platform=spec.platform,
                cap_add=spec.docker_specs.get("run_args", {}).get("cap_add", []),
            )
            container.start()
            logger.info("container %s started for %s", container.id[:12], self.task.id)

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

            test_output, timed_out, _runtime = exec_run_with_timeout(
                container, "/bin/bash /eval.sh", timeout=int(self.timeout_seconds)
            )
            test_output_path = task_output_dir / "test_output.txt"
            test_output_path.write_text(test_output, encoding="utf-8")

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

        logger.info("pulling docker image %s", image)
        try:
            client.images.pull(image)
        except docker.errors.NotFound as exc:
            raise DockerEvaluationError(f"docker image not found: {image}") from exc
        except Exception as exc:
            raise DockerEvaluationError(f"failed to pull docker image {image}: {exc}") from exc

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
                return True
            logger.debug("patch apply attempt failed with '%s':\n%s", cmd, output)

        logger.error("all patch apply attempts failed for %s", self.task.id)
        return False
