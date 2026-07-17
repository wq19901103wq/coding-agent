#!/usr/bin/env python
"""Run a cross-repo sample of SWE-bench cases with resume support.

Runs a representative sample (2 cases per repo, ~24 total) across all 12
repos in the SWE-bench-lite test set, so failure modes aren't biased to a
single repo. Resumes from existing report.json files, so it can be invoked
repeatedly as time permits.

Usage:
    nohup python scripts/run_swe_sample.py > logs/swe-sample.log 2>&1 &

Designed to run detached from any IDE/tool session so the 10-minute tool
timeout does not kill it.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# override=True: .env is the user's latest intent (see agent/repl.py).
load_dotenv(override=True)

# Imports after load_dotenv so modules read env vars at import time.
# noqa: E402 allowed because dotenv must run first.
from agent.config import load_config  # noqa: E402
from swe_bench.dataset import SWEBenchDataset  # noqa: E402
from swe_bench.reporter import JSONReporter, MarkdownReporter  # noqa: E402
from swe_bench.runner import SWEBenchRunner  # noqa: E402

DATASET = "data/swe-bench-lite-test.json"
# Absolute paths: the runner chdirs into cache_dir during git clone, so a
# relative cache_dir would be resolved relative to that new cwd and create
# a nested output/output/... mess. Always use absolute paths here.
_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = str(_ROOT / "output" / "swe-docker-bash-ds-flash-v6")
# Reuse the existing full clones in ~/.coding-agent/swe-bench-cache (created
# by earlier runs) so we don't re-clone multi-hundred-MB repos over a flaky
# GitHub connection.
CACHE_DIR = str(Path.home() / ".coding-agent" / "swe-bench-cache")
PER_REPO = 2  # cases per repo
MODE = "docker-bash"  # "supervisor" or "docker-bash"
TIMEOUT = float(os.environ.get("SWE_BENCH_TIMEOUT", "1200"))  # per-task wall time


def build_sample(dataset: SWEBenchDataset, per_repo: int = PER_REPO) -> list:
    """Pick the first N cases per repo for a balanced cross-repo sample."""
    from collections import defaultdict

    by_repo: dict[str, list] = defaultdict(list)
    for t in dataset.list_tasks():
        by_repo[t.repo].append(t)

    sample: list = []
    for repo in sorted(by_repo):
        sample.extend(by_repo[repo][:per_repo])
    return sample


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    logging.info("model=%s base_url=%s", config.llm.model, config.llm.base_url)

    dataset = SWEBenchDataset(DATASET)
    tasks = build_sample(dataset, PER_REPO)
    logging.info("sample: %d cases across %d repos", len(tasks), len({t.repo for t in tasks}))

    # Resume: tasks with an existing report.json are skipped inside run_dataset.
    already = sum(1 for t in tasks if (Path(OUTPUT_DIR) / t.id / "report.json").exists())
    logging.info("already completed (will skip): %d/%d", already, len(tasks))

    runner = SWEBenchRunner(
        config=config,
        output_dir=OUTPUT_DIR,
        cache_dir=CACHE_DIR,
        use_docker=True,
        timeout_seconds=TIMEOUT,
        mode=MODE,
    )

    start = time.monotonic()
    report = runner.run_dataset(tasks, dataset_path=DATASET)
    elapsed = time.monotonic() - start

    out = Path(OUTPUT_DIR)
    JSONReporter.render(report, out / "report.json")
    MarkdownReporter.render(report, out / "report.md")

    logging.info(
        "DONE in %.0f min: resolved %d/%d (%.1f%%)",
        elapsed / 60,
        report.resolved_count,
        len(report.tasks),
        report.resolution_rate * 100,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
