"""Report generation for SWE-bench benchmark results."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("swe_bench.reporter")


class TaskResult(BaseModel):
    """Result of running a single SWE-bench task."""

    task_id: str
    success: bool
    resolved: bool
    duration_seconds: float
    llm_calls: int = 0
    tool_calls: dict[str, int] = Field(default_factory=dict)
    patch_path: str | None = None
    evaluation_stdout: str = ""
    evaluation_stderr: str = ""
    error: str | None = None


class BenchmarkMetadata(BaseModel):
    """Metadata for a benchmark run."""

    started_at: datetime
    finished_at: datetime
    dataset_path: str
    task_count: int
    model: str | None = None
    provider: str | None = None


class BenchmarkReport(BaseModel):
    """Aggregated report for a benchmark run."""

    metadata: BenchmarkMetadata
    tasks: list[TaskResult]

    @property
    def resolved_count(self) -> int:
        return sum(1 for t in self.tasks if t.resolved)

    @property
    def success_count(self) -> int:
        return sum(1 for t in self.tasks if t.success)

    @property
    def resolution_rate(self) -> float:
        if not self.tasks:
            return 0.0
        return self.resolved_count / len(self.tasks)

    @property
    def avg_duration_seconds(self) -> float:
        if not self.tasks:
            return 0.0
        return sum(t.duration_seconds for t in self.tasks) / len(self.tasks)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        data = super().model_dump(**kwargs)
        data["resolved_count"] = self.resolved_count
        data["success_count"] = self.success_count
        data["resolution_rate"] = self.resolution_rate
        data["avg_duration_seconds"] = self.avg_duration_seconds
        return data


class JSONReporter:
    """Write the report as JSON."""

    @staticmethod
    def render(report: BenchmarkReport, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.model_dump(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logger.info("wrote JSON report to %s", path)

    @staticmethod
    def render_task_result(result: TaskResult, path: Path) -> None:
        """Write a single task result as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    @staticmethod
    def load_task_result(path: Path) -> TaskResult:
        """Load a single task result from JSON."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskResult.model_validate(data)


class MarkdownReporter:
    """Write the report as Markdown."""

    @staticmethod
    def render(report: BenchmarkReport, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = [
            "# SWE-bench Benchmark Report",
            "",
            f"- Dataset: `{report.metadata.dataset_path}`",
            f"- Tasks: {report.metadata.task_count}",
            f"- Started: {report.metadata.started_at.isoformat()}",
            f"- Finished: {report.metadata.finished_at.isoformat()}",
            "",
            "## Summary",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Resolved | {report.resolved_count} / {len(report.tasks)} "
            f"({report.resolution_rate:.1%}) |",
            f"| Success | {report.success_count} / {len(report.tasks)} |",
            f"| Avg Duration | {report.avg_duration_seconds:.2f}s |",
            "",
            "## Tasks",
            "",
            "| Task | Resolved | Duration | Error |",
            "|---|---|---|---|",
        ]
        for task in report.tasks:
            error_cell = task.error or ""
            error_cell = error_cell.replace("|", "\\|").replace("\n", " ")[:80]
            lines.append(
                f"| {task.task_id} | {'✅' if task.resolved else '❌'} | "
                f"{task.duration_seconds:.2f}s | {error_cell} |"
            )
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("wrote Markdown report to %s", path)
