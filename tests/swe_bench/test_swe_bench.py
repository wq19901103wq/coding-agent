"""Tests for SWE-bench integration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from swe_bench.dataset import SWEBenchDataset, SWEBenchTask
from swe_bench.evaluator import SWEBenchEvaluator
from swe_bench.patch_collector import PatchCollector, PatchCollectorError
from swe_bench.reporter import (
    BenchmarkMetadata,
    BenchmarkReport,
    JSONReporter,
    MarkdownReporter,
    TaskResult,
)


@pytest.fixture
def sample_dataset(tmp_path: Path) -> Path:
    """Create a minimal SWE-bench style dataset file."""
    data = [
        {
            "instance_id": "test-repo__1",
            "repo": "owner/repo",
            "base_commit": "abc123",
            "problem_statement": "Fix the add function",
            "test_patch": (
                "diff --git a/test_calc.py b/test_calc.py\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/test_calc.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+def test_add():\n"
                "+    assert True\n"
            ),
        }
    ]
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a small git repository for patch/export tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "test@test.com"])
    _git(repo, ["config", "user.name", "Test"])
    _git(repo, ["add", "."])
    _git(repo, ["commit", "-m", "initial"])
    return repo


def _git(repo: Path, args: list[str]) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_dataset_load_json(sample_dataset: Path) -> None:
    dataset = SWEBenchDataset(sample_dataset)
    tasks = dataset.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "test-repo__1"
    assert tasks[0].repo == "owner/repo"


def test_dataset_filter(sample_dataset: Path) -> None:
    dataset = SWEBenchDataset(sample_dataset)
    assert len(dataset.filter(repo="owner/repo")) == 1
    assert len(dataset.filter(repo="other/repo")) == 0
    assert len(dataset.filter(count=0)) == 0


def test_patch_collector_exports_diff(git_repo: Path) -> None:
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    patch = PatchCollector.export_patch(git_repo)
    assert "-    return a - b" in patch
    assert "+    return a + b" in patch


def test_patch_collector_requires_git(tmp_path: Path) -> None:
    with pytest.raises(PatchCollectorError):
        PatchCollector.export_patch(tmp_path)


def test_evaluator_resolves_patch(git_repo: Path) -> None:
    task = SWEBenchTask.model_validate(
        {
            "instance_id": "test__1",
            "repo": "owner/repo",
            "base_commit": "HEAD",
            "problem_statement": "fix add",
            "test_patch": None,
        }
    )
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (git_repo / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    patch = PatchCollector.export_patch(git_repo)

    # Reset so evaluator applies the patch from a clean base.
    _git(git_repo, ["checkout", "-f", "HEAD"])
    _git(git_repo, ["clean", "-fd"])

    evaluator = SWEBenchEvaluator(task, timeout_seconds=30.0)
    result = evaluator.evaluate(patch, git_repo)
    assert result.success
    assert result.resolved


def test_evaluator_fails_on_bad_patch(git_repo: Path) -> None:
    task = SWEBenchTask.model_validate(
        {
            "instance_id": "test__2",
            "repo": "owner/repo",
            "base_commit": "HEAD",
            "problem_statement": "fix add",
            "test_patch": None,
        }
    )
    evaluator = SWEBenchEvaluator(task, timeout_seconds=30.0)
    result = evaluator.evaluate("this is not a valid patch", git_repo)
    assert not result.success
    assert not result.resolved


def test_report_aggregation(tmp_path: Path) -> None:
    from datetime import datetime

    tasks = [
        TaskResult(task_id="t1", success=True, resolved=True, duration_seconds=10.0),
        TaskResult(
            task_id="t2", success=False, resolved=False, duration_seconds=20.0, error="timeout"
        ),
    ]
    report = BenchmarkReport(
        metadata=BenchmarkMetadata(
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
            dataset_path="data/test.json",
            task_count=2,
        ),
        tasks=tasks,
    )
    assert report.resolved_count == 1
    assert report.resolution_rate == 0.5
    assert report.avg_duration_seconds == 15.0

    JSONReporter.render(report, tmp_path / "report.json")
    assert (tmp_path / "report.json").exists()

    MarkdownReporter.render(report, tmp_path / "report.md")
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "t1" in md
    assert "t2" in md


def test_task_model_alias() -> None:
    task = SWEBenchTask.model_validate(
        {
            "instance_id": "x-1",
            "repo": "a/b",
            "base_commit": "c1",
            "problem_statement": "fix it",
        }
    )
    assert task.id == "x-1"
    assert task.issue_title == "fix it"
