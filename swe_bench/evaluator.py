"""Evaluate an agent-generated patch for a SWE-bench task."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

from swe_bench.dataset import SWEBenchTask

logger = logging.getLogger("swe_bench.evaluator")


class EvaluationError(Exception):
    """Raised when evaluation cannot be completed."""


class EvaluationResult(BaseModel):
    success: bool
    resolved: bool
    stdout: str
    stderr: str
    exit_code: int | None
    error: str | None


class SWEBenchEvaluator:
    """Evaluate a patch by applying it and running the test suite."""

    def __init__(self, task: SWEBenchTask, timeout_seconds: float = 300.0) -> None:
        self.task = task
        self.timeout_seconds = timeout_seconds

    def evaluate(self, patch: str, workspace: Path) -> EvaluationResult:
        """Apply ``patch`` and run tests in ``workspace``.

        If the task provides a ``test_patch``, it is applied after the agent
        patch to introduce the new/regression tests.
        """
        if not (workspace / ".git").exists():
            return _error_result("workspace is not a git repository")

        # Reset to base commit to ensure clean state.
        _git(workspace, ["reset", "--hard", self.task.base_commit], check=True)
        _git(workspace, ["clean", "-fd"], check=False)

        # Apply agent patch.
        apply_result = _apply_patch(workspace, patch)
        if not apply_result.success:
            logger.error("failed to apply agent patch: %s", apply_result.error)
            return apply_result

        # Apply test patch if present.
        if self.task.test_patch:
            test_apply = _apply_patch(workspace, self.task.test_patch)
            if not test_apply.success:
                logger.error("failed to apply test patch: %s", test_apply.error)
                return test_apply

        # Run tests.
        return _run_tests(workspace, self.timeout_seconds)


def _apply_patch(workspace: Path, patch: str) -> EvaluationResult:
    if not patch.strip():
        return EvaluationResult(
            success=True,
            resolved=False,
            stdout="",
            stderr="",
            exit_code=0,
            error=None,
        )

    git_apply = subprocess.run(
        ["git", "-C", str(workspace), "apply", "--check"],
        input=patch,
        text=True,
        capture_output=True,
        check=False,
    )
    if git_apply.returncode != 0:
        return EvaluationResult(
            success=False,
            resolved=False,
            stdout=git_apply.stdout,
            stderr=git_apply.stderr,
            exit_code=git_apply.returncode,
            error="patch does not apply cleanly",
        )

    result = subprocess.run(
        ["git", "-C", str(workspace), "apply"],
        input=patch,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return EvaluationResult(
            success=False,
            resolved=False,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            error="failed to apply patch",
        )

    return EvaluationResult(
        success=True,
        resolved=False,
        stdout="",
        stderr="",
        exit_code=0,
        error=None,
    )


def _run_tests(workspace: Path, timeout_seconds: float) -> EvaluationResult:
    pytest_path = shutil.which("pytest") or shutil.which("py.test")
    if pytest_path is None:
        return _error_result("pytest not found in PATH")

    try:
        result = subprocess.run(
            [pytest_path, "-q", "--tb=short"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return EvaluationResult(
            success=False,
            resolved=False,
            stdout=stdout,
            stderr=stderr,
            exit_code=None,
            error=f"test execution timed out after {timeout_seconds}s",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return _error_result(f"test execution failed: {exc}")

    resolved = result.returncode == 0
    return EvaluationResult(
        success=True,
        resolved=resolved,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
        error=None,
    )


def _error_result(error: str) -> EvaluationResult:
    logger.error("%s", error)
    return EvaluationResult(
        success=False,
        resolved=False,
        stdout="",
        stderr="",
        exit_code=None,
        error=error,
    )


def _git(
    workspace: Path, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(workspace), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise EvaluationError(
            f"git command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result
