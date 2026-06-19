"""Command-line interface for SWE-bench benchmark runner."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agent.config import load_config
from swe_bench.dataset import SWEBenchDataset
from swe_bench.reporter import JSONReporter, MarkdownReporter
from swe_bench.runner import SWEBenchRunner


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run coding-agent on SWE-bench tasks.",
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Path to SWE-bench dataset (JSON or JSONL).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output directory for results and reports.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to coding-agent config file.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of tasks to run.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Offset into the dataset before selecting tasks.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Filter tasks to a specific repo (e.g. django/django).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Per-task timeout in seconds.",
    )
    parser.add_argument(
        "--mock-responses",
        default=None,
        help="Path to mock LLM responses JSON (for pipeline regression testing).",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Directory to cache cloned repositories.",
    )
    parser.add_argument(
        "--report-formats",
        default="json,markdown",
        help="Comma-separated report formats (json,markdown).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(config_path=args.config)
    if not config.llm.api_key and args.mock_responses is None:
        logging.warning(
            "no LLM API key configured; set CODING_AGENT_LLM_API_KEY or use --mock-responses"
        )

    dataset = SWEBenchDataset(args.dataset)
    tasks = dataset.filter(repo=args.repo, count=args.limit, offset=args.offset)
    if not tasks:
        logging.error("no tasks selected from dataset")
        return 1

    logging.info("selected %d tasks", len(tasks))

    runner = SWEBenchRunner(
        config=config,
        output_dir=args.output,
        cache_dir=args.cache_dir,
        timeout_seconds=args.timeout,
        mock_responses=args.mock_responses,
    )

    report = runner.run_dataset(tasks, dataset_path=args.dataset)

    output_dir = Path(args.output)
    formats = {f.strip().lower() for f in args.report_formats.split(",")}
    if "json" in formats:
        JSONReporter.render(report, output_dir / "report.json")
    if "markdown" in formats:
        MarkdownReporter.render(report, output_dir / "report.md")

    logging.info(
        "benchmark complete: resolved %d/%d (%.1f%%)",
        report.resolved_count,
        len(report.tasks),
        report.resolution_rate * 100,
    )

    return 0 if report.resolved_count == len(report.tasks) else 1


if __name__ == "__main__":
    sys.exit(main())
