"""Tests for SWE-bench integration."""

from __future__ import annotations

import json
import shutil
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
            "FAIL_TO_PASS": ["test_calc.py::test_add"],
            "PASS_TO_PASS": [],
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


def test_fail_to_pass_json_string_parsing() -> None:
    """FAIL_TO_PASS/PASS_TO_PASS may be JSON-encoded strings in the dataset."""
    task = SWEBenchTask.model_validate(
        {
            "instance_id": "x-2",
            "repo": "a/b",
            "base_commit": "c1",
            "problem_statement": "fix it",
            "FAIL_TO_PASS": json.dumps(["tests/test_foo.py::test_bar"]),
            "PASS_TO_PASS": json.dumps(["tests/test_foo.py::test_baz"]),
        }
    )
    assert task.fail_to_pass == ["tests/test_foo.py::test_bar"]
    assert task.pass_to_pass == ["tests/test_foo.py::test_baz"]


def test_evaluator_uses_official_cases(git_repo: Path) -> None:
    """Evaluator should run FAIL_TO_PASS and PASS_TO_PASS cases only."""
    # Base code has a bug; test_failing is the official FAIL_TO_PASS case.
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (git_repo / "test_calc.py").write_text(
        "from calc import add\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "def test_multiply():\n"
        "    assert 2 * 3 == 6\n",
        encoding="utf-8",
    )
    _git(git_repo, ["add", "."])
    _git(git_repo, ["commit", "-m", "buggy"])

    # Test patch introduces an extra regression test.
    test_patch = (
        "diff --git a/test_regression.py b/test_regression.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/test_regression.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_regression():\n"
        "+    assert True\n"
    )

    task = SWEBenchTask.model_validate(
        {
            "instance_id": "test__official",
            "repo": "owner/repo",
            "base_commit": "HEAD",
            "problem_statement": "fix add",
            "test_patch": test_patch,
            "FAIL_TO_PASS": ["test_calc.py::test_add"],
            "PASS_TO_PASS": ["test_calc.py::test_multiply", "test_regression.py::test_regression"],
        }
    )

    # Prepare a fixed patch.
    fixed_repo = git_repo.parent / "fixed"
    shutil.copytree(git_repo, fixed_repo)
    (fixed_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    patch = PatchCollector.export_patch(fixed_repo)

    evaluator = SWEBenchEvaluator(task, timeout_seconds=30.0)
    result = evaluator.evaluate(patch, git_repo)
    assert result.success
    assert result.resolved
    assert "FAIL_TO_PASS" in result.stdout
    assert "PASS_TO_PASS" in result.stdout


def test_evaluator_not_resolved_when_fail_to_pass_fails(git_repo: Path) -> None:
    """If FAIL_TO_PASS cases still fail, the task should not be resolved."""
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (git_repo / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(git_repo, ["add", "."])
    _git(git_repo, ["commit", "-m", "buggy"])

    task = SWEBenchTask.model_validate(
        {
            "instance_id": "test__unresolved",
            "repo": "owner/repo",
            "base_commit": "HEAD",
            "problem_statement": "fix add",
            "FAIL_TO_PASS": ["test_calc.py::test_add"],
            "PASS_TO_PASS": [],
        }
    )

    evaluator = SWEBenchEvaluator(task, timeout_seconds=30.0)
    result = evaluator.evaluate("", git_repo)
    assert result.success
    assert not result.resolved
