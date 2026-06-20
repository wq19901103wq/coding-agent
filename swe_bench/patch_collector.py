"""Export a standard unified diff patch from a workspace."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("swe_bench.patch_collector")


class PatchCollectorError(Exception):
    """Raised when patch collection fails."""


class PatchCollector:
    """Collect a git diff patch from a modified workspace."""

    @staticmethod
    def export_patch(workspace: Path, base_ref: str = "HEAD") -> str:
        """Return a unified diff of ``workspace`` relative to ``base_ref``.

        Untracked files are included as new files. The caller is responsible for
        ensuring ``workspace`` is a git repository.
        """
        if not (workspace / ".git").exists():
            raise PatchCollectorError(f"workspace is not a git repository: {workspace}")

        # Remove build artifacts and cache directories that may be created
        # during testing so they do not pollute the exported patch.
        _clean_artifacts(workspace)

        # Stage untracked files so they appear in the diff.
        _git(workspace, ["add", "--intent-to-add", "."], check=False)

        result = _git(workspace, ["diff", "--no-color"], check=True, capture_output=True)
        patch = result.stdout
        if not patch.strip():
            logger.warning("empty patch for workspace %s", workspace)
        return patch

    @staticmethod
    def write_patch(workspace: Path, output_path: Path, base_ref: str = "HEAD") -> None:
        """Export and write the patch to ``output_path``."""
        patch = PatchCollector.export_patch(workspace, base_ref)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(patch, encoding="utf-8")
        logger.info("wrote patch to %s", output_path)


def _clean_artifacts(workspace: Path) -> None:
    """Remove common test/build artifacts from the workspace before diffing."""
    for pattern in ("__pycache__", "*.pyc", "*.pyo", ".pytest_cache"):
        for path in workspace.rglob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)


def _git(
    cwd: Path,
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(cwd), *args]
    logger.debug("running %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=capture_output,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise PatchCollectorError(
            f"git command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result
