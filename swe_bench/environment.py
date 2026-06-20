"""Prepare per-task conda environments using official SWE-bench specs."""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from swebench.harness.test_spec.test_spec import make_test_spec

from swe_bench.dataset import SWEBenchTask

logger = logging.getLogger("swe_bench.environment")


class EnvironmentBuildError(Exception):
    """Raised when the conda environment cannot be prepared."""


class CondaEnvironmentBuilder:
    """Build/activate a conda environment matching the official SWE-bench spec.

    The builder uses ``swebench`` to generate the official environment and
    repository installation scripts, then rewrites them for the local machine:

    - replaces the hard-coded ``/opt/miniconda3`` conda prefix with the local
      Anaconda/Miniconda installation;
    - replaces the hard-coded ``/testbed`` directory with the task workspace;
    - gives each task a unique conda environment name so different Python
      versions do not collide.
    """

    def __init__(
        self,
        task: SWEBenchTask,
        workspace: Path,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.task = task
        self.workspace = Path(workspace)
        self.cache_dir = Path(
            cache_dir if cache_dir else Path.home() / ".coding-agent" / "swe-bench-envs"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.conda_prefix = self._find_conda_prefix()

    def env_name(self) -> str:
        """Return a unique conda env name for this task."""
        repo = self.task.repo.replace("/", "__")
        version = self.task.version or "unknown"
        setup_commit = self.task.environment_setup_commit or self.task.base_commit
        unique = f"{repo}@{version}@{setup_commit}"
        hash_suffix = hashlib.md5(unique.encode("utf-8")).hexdigest()[:12]
        return f"swe_{repo}_{hash_suffix}"

    def prepare(self, timeout_seconds: float = 1800.0) -> str:
        """Create the conda env and install/build the repo.

        Returns the name of the prepared conda environment.
        """
        env_name = self.env_name()
        spec = make_test_spec(self.task.to_instance_dict())

        if not self._env_exists(env_name):
            logger.info(
                "creating conda env %s for %s (python %s)",
                env_name,
                self.task.id,
                spec.version,
            )
            env_script = self._rewrite_script(spec.env_script_list, env_name)
            self._run_script(env_script, "env setup", timeout_seconds)
        else:
            logger.info("reusing existing conda env %s for %s", env_name, self.task.id)

        logger.info("installing repo %s in conda env %s", self.task.repo, env_name)
        repo_script = self._rewrite_script(spec.repo_script_list, env_name)
        self._run_script(repo_script, "repo install", timeout_seconds)

        return env_name

    def _find_conda_prefix(self) -> Path:
        """Locate the base conda installation."""
        conda_exe = shutil.which("conda") or os.environ.get("CONDA_EXE")
        if conda_exe is None:
            raise EnvironmentBuildError("conda executable not found in PATH")
        # ``conda`` is usually at ``<prefix>/bin/conda`` or ``<prefix>/condabin/conda``.
        prefix = Path(conda_exe).resolve().parent.parent
        if (
            not (prefix / "bin" / "activate").exists()
            and not (prefix / "etc" / "profile.d" / "conda.sh").exists()
        ):
            raise EnvironmentBuildError(f"cannot locate conda prefix from {conda_exe}")
        return prefix

    def _env_exists(self, env_name: str) -> bool:
        """Check whether a conda environment already exists."""
        result = subprocess.run(
            ["conda", "env", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        try:
            import json

            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        for env in data.get("envs", []):
            if Path(env).name == env_name:
                return True
        return False

    def _rewrite_script(self, commands: list[str], env_name: str) -> str:
        """Rewrite swebench commands for the local conda and workspace."""
        activate = self.conda_prefix / "bin" / "activate"
        conda_sh = self.conda_prefix / "etc" / "profile.d" / "conda.sh"
        activate_line = f"source {activate}" if activate.exists() else f"source {conda_sh}"

        rewritten: list[str] = [
            "#!/bin/bash",
            "set -e",
            activate_line,
        ]
        if platform.system() == "Darwin":
            # macOS clang treats several warnings as errors for these older
            # codebases; relax them so C extensions can build.
            rewritten.append(
                'export CFLAGS="-Wno-error -Wno-error=incompatible-function-pointer-types '
                '-Wno-error=int-conversion"'
            )
        workspace = str(self.workspace)

        for cmd in commands:
            raw = cmd.strip()
            # The workspace is already prepared by the runner; skip clone.
            if raw.startswith("git clone"):
                continue
            # The timestamp / future-commit checks use GNU date syntax that is
            # unavailable on macOS and are unnecessary for local evaluation.
            if "AFTER_TIMESTAMP" in raw or "COMMIT_COUNT" in raw:
                continue
            if raw.startswith('[ "$COMMIT_COUNT"'):
                continue
            # Replace official miniconda prefix with local prefix.
            cmd = cmd.replace("/opt/miniconda3", str(self.conda_prefix))
            # Replace the official env name with our unique env name.
            cmd = self._replace_env_name(cmd, env_name)
            # Replace /testbed with the actual workspace path.
            cmd = cmd.replace("/testbed", workspace)
            # macOS ``sed -i`` requires an empty backup extension argument.
            if platform.system() == "Darwin" and cmd.startswith("sed -i '"):
                cmd = cmd.replace("sed -i '", "sed -i '' '", 1)
            rewritten.append(cmd)

        return "\n".join(rewritten) + "\n"

    def _replace_env_name(self, cmd: str, env_name: str) -> str:
        """Replace the generic ``testbed`` env name with a unique one."""
        lowered = cmd.lower()
        if "testbed" not in lowered:
            return cmd
        if "conda" not in lowered and "mamba" not in lowered:
            return cmd
        import re

        return re.sub(r"\btestbed\b", env_name, cmd)

    def _run_script(
        self,
        script: str,
        label: str,
        timeout_seconds: float,
    ) -> None:
        """Execute a bash script and raise on failure."""
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".sh",
            prefix=f"swe_{self.task.id}_{label}_",
            dir=self.cache_dir,
            delete=False,
        ) as f:
            f.write(script)
            script_path = Path(f.name)

        logger.debug("wrote %s script to %s", label, script_path)
        try:
            result = subprocess.run(
                ["bash", str(script_path)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode("utf-8") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            )
            raise EnvironmentBuildError(
                f"{label} timed out after {timeout_seconds}s\nstdout: {stdout}\nstderr: {stderr}"
            ) from exc

        if result.returncode != 0:
            tail = result.stderr[-4000:] if len(result.stderr) > 4000 else result.stderr
            raise EnvironmentBuildError(
                f"{label} failed with exit code {result.returncode}\n"
                f"stdout: {result.stdout[-4000:]}\n"
                f"stderr: {tail}"
            )

        logger.info("%s completed successfully for %s", label, self.task.id)
