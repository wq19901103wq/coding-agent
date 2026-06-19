"""SWE-bench dataset loading and task representation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("swe_bench.dataset")


class SWEBenchTask(BaseModel):
    """A single SWE-bench task instance."""

    id: str = Field(..., alias="instance_id")
    repo: str
    base_commit: str
    issue_title: str = Field(default="", alias="problem_statement")
    issue_body: str = ""
    test_patch: str | None = None
    patch: str | None = Field(default=None, alias="patch")
    environment_setup_commit: str | None = Field(default=None, alias="environment_setup_commit")
    hints_text: str | None = Field(default=None, alias="hints_text")
    version: str | None = None

    model_config = {"populate_by_name": True}


class SWEBenchDataset:
    """Loads and filters SWE-bench style datasets."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._tasks: list[SWEBenchTask] | None = None

    def _load_raw(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise FileNotFoundError(f"dataset not found: {self.path}")

        if self.path.suffix == ".jsonl":
            tasks: list[dict[str, Any]] = []
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        tasks.append(json.loads(line))
            return tasks

        with self.path.open(encoding="utf-8") as f:
            data: list[dict[str, Any]] | dict[str, Any] = json.load(f)
        if isinstance(data, dict):
            return list(data.values())
        return data

    def list_tasks(self) -> list[SWEBenchTask]:
        """Return all tasks in the dataset."""
        if self._tasks is None:
            raw_tasks = self._load_raw()
            self._tasks = [SWEBenchTask.model_validate(t) for t in raw_tasks]
            logger.info("loaded %d tasks from %s", len(self._tasks), self.path)
        return self._tasks

    def filter(
        self,
        repo: str | None = None,
        count: int | None = None,
        offset: int = 0,
    ) -> list[SWEBenchTask]:
        """Filter tasks by repo and/or limit count."""
        tasks = self.list_tasks()
        if repo is not None:
            tasks = [t for t in tasks if t.repo == repo]
        tasks = tasks[offset:]
        if count is not None:
            tasks = tasks[:count]
        return tasks
