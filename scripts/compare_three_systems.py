#!/usr/bin/env python3
"""A/B/C comparison: coding-agent direct vs Claude Code vs SWE-agent (semi-official).

All three use `deepseek-v4-flash`. Evaluation is done with coding-agent's
DockerEvaluator to keep scoring consistent.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent.config import Config, load_config  # noqa: E402
from swe_bench.dataset import SWEBenchDataset, SWEBenchTask  # noqa: E402
from swe_bench.docker import DockerEvaluator  # noqa: E402
from swe_bench.runner import SWEBenchRunner  # noqa: E402

logger = logging.getLogger("compare_three_systems")

DEFAULT_TASKS = [
    "pytest-dev__pytest-11143",
    "pytest-dev__pytest-11148",
    "pytest-dev__pytest-5103",
    "pytest-dev__pytest-5221",
    "pytest-dev__pytest-5227",
    "pytest-dev__pytest-5413",
    "pytest-dev__pytest-5495",
    "pytest-dev__pytest-5692",
    "pytest-dev__pytest-6116",
    "pytest-dev__pytest-7168",
    "astropy__astropy-12907",
    "astropy__astropy-14182",
    "django__django-10914",
    "django__django-10924",
    "matplotlib__matplotlib-18869",
    "matplotlib__matplotlib-22711",
    "mwaskom__seaborn-2848",
    "mwaskom__seaborn-3010",
    "pallets__flask-4045",
    "pallets__flask-4992",
]

SWE_AGENT_ENV = os.environ.get(
    "SWE_AGENT_ENV",
    str(Path.home() / "anaconda3" / "envs" / "swe_agent_py311"),
)
SWE_AGENT_RUNNER = REPO_ROOT / "swe_agent_local_runner.py"


@dataclass
class ComparisonResult:
    task_id: str
    direct_resolved: bool | None
    direct_duration: float | None
    direct_error: str | None
    claude_resolved: bool | None
    claude_duration: float | None
    claude_error: str | None
    swe_agent_resolved: bool | None
    swe_agent_duration: float | None
    swe_agent_error: str | None


def load_tasks(dataset_path: str, task_ids: list[str]) -> list[SWEBenchTask]:
    ds = SWEBenchDataset(dataset_path)
    by_id = {t.id: t for t in ds.list_tasks()}
    missing = [tid for tid in task_ids if tid not in by_id]
    if missing:
        raise ValueError(f"unknown task ids: {missing}")
    return [by_id[tid] for tid in task_ids]


def build_goal_description(task: SWEBenchTask) -> str:
    """Mirror SWEBenchRunner._build_goal_description for consistency."""
    parts: list[str] = []
    if task.issue_title:
        parts.append(f"Title: {task.issue_title}")
    if task.issue_body:
        parts.append(f"Description:\n{task.issue_body}")
    if task.hints_text:
        parts.append(f"Hints: {task.hints_text}")

    instructions = (
        "You are fixing a real bug in this repository.\n\n"
        "Use the problem statement above to understand the required behavior "
        "and fix the source code accordingly. Hidden verification tests are "
        "available only to the evaluation harness.\n\n"
        "## Workflow (do this efficiently)\n"
        "1. Read the problem statement above. Understand what the bug is.\n"
        "2. Find the relevant source files using code_search or glob_search.\n"
        "3. Read the source code carefully and identify the root cause.\n"
        "4. Apply the smallest possible fix using str_replace_file.\n"
        "5. Verify your fix with relevant existing tests or a focused reproduction.\n\n"
        "## Rules\n"
        "- NEVER modify pyproject.toml, setup.cfg, setup.py, tox.ini, Makefile, "
        "or any config file.\n"
        "- NEVER modify or add test files under testing/ or tests/.\n"
        "- NEVER run pip install, conda install, or any package manager.\n"
        "- NEVER debug the environment — if a command fails, try a different approach.\n"
        "- Make the MINIMAL change — edit only the few lines that cause the bug.\n"
        "- Do NOT commit, create a branch, or use git stash/reset/revert.\n"
        "- Work in the current directory; do NOT cd to /home/user or other paths.\n"
        "- You MUST verify the fix before reporting completion. Do not skip verification."
    )
    parts.append(instructions)
    return "\n\n".join(parts)


def prepare_workspace(task: SWEBenchTask, workspace: Path, cache_dir: Path | None = None) -> None:
    if cache_dir is None:
        cache_dir = Path.home() / ".coding-agent" / "swe-bench-cache"
    repo_cache = cache_dir / task.repo.replace("/", "__")
    if not repo_cache.exists():
        raise FileNotFoundError(f"repo cache not found: {repo_cache}")

    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(repo_cache, workspace)

    try:
        subprocess.run(
            ["git", "cat-file", "-t", task.base_commit],
            cwd=workspace,
            capture_output=True,
            timeout=5,
            check=True,
        )
    except Exception:
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", task.base_commit],
            cwd=workspace,
            capture_output=True,
            timeout=60,
            check=False,
        )

    subprocess.run(
        ["git", "checkout", "-f", task.base_commit],
        cwd=workspace,
        check=True,
        capture_output=True,
        timeout=600,
    )
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=workspace,
        check=True,
        capture_output=True,
        timeout=300,
    )


def run_claude(
    task: SWEBenchTask,
    task_output_dir: Path,
    workspace: Path,
    model: str,
    timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    start = time.monotonic()
    task_output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = task_output_dir / "prompt.txt"
    prompt_path.write_text(build_goal_description(task), encoding="utf-8")

    env = dict(os.environ)
    env["CLAUDE_CODE_DEBUG"] = "1"
    # Use the same underlying deepseek-v4-flash model via cc-switch.
    # cc-switch recognizes the Anthropic-style model id with a [1m] suffix.
    env["ANTHROPIC_MODEL"] = model if model.endswith("[1m]") else f"{model}[1m]"
    env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:15721/v1"
    env["ANTHROPIC_API_KEY"] = "placeholder"
    env["ANTHROPIC_MAX_TOKENS"] = "64000"

    stdout_path = task_output_dir / "claude.out"
    stderr_path = task_output_dir / "claude.err"

    proc = subprocess.Popen(
        [
            "claude",
            "-p",
            "--verbose",
            "Fix the bug described in ../prompt.txt. Read the file for the prompt. "
            "Make minimal changes to src/pytest to make the failing tests pass. "
            "Stop after editing.",
        ],
        cwd=str(workspace),
        env=env,
        stdout=open(stdout_path, "w", encoding="utf-8"),
        stderr=open(stderr_path, "w", encoding="utf-8"),
        text=True,
    )
    try:
        proc.wait(timeout=timeout_seconds)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        exit_code = -1

    duration = time.monotonic() - start

    patch = subprocess.run(
        ["git", "-C", str(workspace), "diff"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout

    if exit_code != 0:
        return {
            "resolved": False,
            "duration": duration,
            "error": f"claude exit code {exit_code}",
            "patch": patch,
        }
    if not patch.strip():
        return {"resolved": False, "duration": duration, "error": "empty patch", "patch": patch}

    (task_output_dir / "agent.patch").write_text(patch, encoding="utf-8")
    return evaluate_patch(task, workspace, patch, task_output_dir / "docker_eval")


def run_direct(
    task: SWEBenchTask,
    task_output_dir: Path,
    workspace: Path,
    config: Config,
    model: str,
    timeout_seconds: float = 1200.0,
) -> dict[str, Any]:
    start = time.monotonic()
    # Temporarily override model in config.
    original_model = config.llm.model
    config.llm.model = model
    try:
        runner = SWEBenchRunner(
            config=config,
            output_dir=task_output_dir,
            use_docker=True,
            mode="direct",
            timeout_seconds=timeout_seconds,
        )
        from swe_bench.reporter import TaskResult

        result: TaskResult = runner.run_task(task)
        patch = ""
        if result.patch_path and Path(result.patch_path).exists():
            patch = Path(result.patch_path).read_text(encoding="utf-8")
        return {
            "resolved": result.resolved,
            "duration": time.monotonic() - start,
            "error": result.error,
            "patch": patch,
        }
    finally:
        config.llm.model = original_model


def run_swe_agent(
    task: SWEBenchTask,
    task_output_dir: Path,
    workspace: Path,
    model: str,
    timeout_seconds: float = 1200.0,
    max_steps: int = 100,
    timeout_per_command: int = 300,
) -> dict[str, Any]:
    start = time.monotonic()
    task_output_dir.mkdir(parents=True, exist_ok=True)

    # 清理旧 patch，避免本次运行 crash / 空 patch 时复用上一轮的 agent.patch。
    stale_patch = task_output_dir / "agent.patch"
    if stale_patch.exists():
        stale_patch.unlink()
    # 同时清理 runner 子目录，防止旧 agent.patch 被误读。
    run_dir = task_output_dir / "swe_agent_run"
    if run_dir.exists():
        shutil.rmtree(run_dir)

    cmd = [
        f"{SWE_AGENT_ENV}/bin/python",
        str(SWE_AGENT_RUNNER),
        "--task-id",
        task.id,
        "--workspace",
        str(workspace),
        "--output-dir",
        str(task_output_dir / "swe_agent_run"),
        "--model",
        model,
        "--max-steps",
        str(max_steps),
        "--timeout-per-command",
        str(timeout_per_command),
    ]
    env = dict(os.environ)
    env["PATH"] = f"{SWE_AGENT_ENV}/bin:" + env.get("PATH", "")
    # SWE-agent's DeepSeekModel reads API config from keys.cfg/env.
    env.setdefault(
        "DEEPSEEK_API_BASE_URL", env.get("CODING_AGENT_LLM_BASE_URL", "https://api.deepseek.com/v1")
    )
    env.setdefault("DEEPSEEK_API_KEY", env.get("CODING_AGENT_LLM_API_KEY", ""))

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=open(task_output_dir / "swe_agent.out", "w", encoding="utf-8"),
        stderr=open(task_output_dir / "swe_agent.err", "w", encoding="utf-8"),
        text=True,
    )
    try:
        proc.wait(timeout=timeout_seconds)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        exit_code = -1

    duration = time.monotonic() - start
    patch_path = task_output_dir / "swe_agent_run" / "agent.patch"
    patch = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""

    if exit_code != 0:
        return {
            "resolved": False,
            "duration": duration,
            "error": f"swe-agent exit code {exit_code}",
            "patch": patch,
        }
    if not patch.strip():
        return {"resolved": False, "duration": duration, "error": "empty patch", "patch": patch}

    (task_output_dir / "agent.patch").write_text(patch, encoding="utf-8")
    return evaluate_patch(task, workspace, patch, task_output_dir / "docker_eval")


INFRA_ERROR_PATTERNS = (
    "ModuleNotFoundError",
    "ImportError",
    "No module named",
    "can't open file",
)


def is_infra_error(error: str | None) -> bool:
    if not error:
        return False
    return any(p.lower() in error.lower() for p in INFRA_ERROR_PATTERNS)


def evaluate_patch(
    task: SWEBenchTask, workspace: Path, patch: str, eval_output_dir: Path
) -> dict[str, Any]:
    if not patch.strip():
        return {"resolved": False, "duration": 0.0, "error": "empty patch", "patch": patch}
    evaluator = DockerEvaluator(task, timeout_seconds=300, output_dir=eval_output_dir)
    eval_result = evaluator.evaluate(patch, workspace=workspace)
    return {
        "resolved": eval_result.resolved,
        "duration": None,  # caller adds generation duration separately
        "error": eval_result.error,
        "patch": patch,
    }


def render_table(results: list[ComparisonResult]) -> str:
    lines = [
        "| task | direct | Claude | SWE-agent |",
        "|------|--------|--------|-----------|",
    ]
    for r in results:
        lines.append(
            f"| {r.task_id} | "
            f"{r.direct_resolved if r.direct_resolved is not None else '-'} | "
            f"{r.claude_resolved if r.claude_resolved is not None else '-'} | "
            f"{r.swe_agent_resolved if r.swe_agent_resolved is not None else '-'} |"
        )
    lines.append("")
    lines.append(
        f"**direct resolved:** {sum(1 for r in results if r.direct_resolved)}/{len(results)}"
    )
    lines.append(
        f"**Claude resolved:** {sum(1 for r in results if r.claude_resolved)}/{len(results)}"
    )
    lines.append(
        f"**SWE-agent resolved:** {sum(1 for r in results if r.swe_agent_resolved)}/{len(results)}"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["direct", "claude", "swe-agent", "all"], default="all")
    parser.add_argument("--dataset", default="data/swe-bench-lite-test.json")
    parser.add_argument("--tasks", nargs="+", default=DEFAULT_TASKS)
    parser.add_argument("--output-dir", default="output/compare-three-systems-flash")
    parser.add_argument("--config", default=None)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--sample-size", type=int, default=0, help="If >0, randomly sample N tasks")
    parser.add_argument(
        "--max-workers", type=int, default=1, help="Concurrent tasks (Claude always sequential)"
    )
    parser.add_argument(
        "--rerun-failed",
        action="store_true",
        help="Re-run systems whose previous result was not resolved",
    )
    parser.add_argument(
        "--swe-agent-timeout", type=int, default=1200, help="Per-task timeout for SWE-agent (s)"
    )
    parser.add_argument(
        "--swe-agent-max-steps", type=int, default=100, help="Max SWE-agent steps per task"
    )
    parser.add_argument(
        "--swe-agent-timeout-per-command",
        type=int,
        default=300,
        help="Per-command bash timeout for SWE-agent (s)",
    )
    args = parser.parse_args()

    if args.mode in ("swe-agent", "all") and not SWE_AGENT_RUNNER.exists():
        parser.error(f"SWE-agent runner not found: {SWE_AGENT_RUNNER}")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = load_tasks(args.dataset, args.tasks)
    if args.sample_size > 0:
        import random

        random.seed(42)
        tasks = random.sample(tasks, min(args.sample_size, len(tasks)))

    results: list[ComparisonResult] = [
        ComparisonResult(
            task_id=t.id,
            direct_resolved=None,
            direct_duration=None,
            direct_error=None,
            claude_resolved=None,
            claude_duration=None,
            claude_error=None,
            swe_agent_resolved=None,
            swe_agent_duration=None,
            swe_agent_error=None,
        )
        for t in tasks
    ]
    by_id = {r.task_id: r for r in results}

    # Load .env so CODING_AGENT_LLM_* are available.
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    config = load_config(args.config) if args.mode in ("direct", "all") else None

    for task in tasks:
        r = by_id[task.id]
        task_output_dir = output_dir / task.id
        task_output_dir.mkdir(parents=True, exist_ok=True)

        # Load any partial result from a previous run so we can resume.
        comparison_path = task_output_dir / "comparison.json"
        if comparison_path.exists():
            try:
                existing = json.loads(comparison_path.read_text(encoding="utf-8"))
                for key, value in existing.items():
                    if hasattr(r, key):
                        setattr(r, key, value)
                logger.info("loaded partial result for %s", task.id)
            except Exception:
                pass

        # Prepare one workspace per system so they don't interfere.
        if args.mode in ("direct", "all") and (
            r.direct_resolved is None or (args.rerun_failed and r.direct_resolved is not True)
        ):
            workspace = task_output_dir / "direct_workspace"
            prepare_workspace(task, workspace)
            logger.info("running direct for %s", task.id)
            direct = run_direct(task, task_output_dir / "direct", workspace, config, args.model)  # type: ignore[arg-type]
            r.direct_resolved = direct["resolved"]
            r.direct_duration = direct["duration"]
            r.direct_error = direct["error"]
            logger.info("direct %s -> resolved=%s", task.id, r.direct_resolved)

        if args.mode in ("claude", "all") and (
            r.claude_resolved is None or (args.rerun_failed and r.claude_resolved is not True)
        ):
            workspace = task_output_dir / "claude_workspace"
            prepare_workspace(task, workspace)
            logger.info("running Claude Code for %s", task.id)
            claude = run_claude(task, task_output_dir / "claude", workspace, args.model)
            r.claude_resolved = claude["resolved"]
            r.claude_duration = claude["duration"]
            r.claude_error = claude["error"]
            logger.info("Claude %s -> resolved=%s", task.id, r.claude_resolved)

        if args.mode in ("swe-agent", "all") and (
            r.swe_agent_resolved is None or (args.rerun_failed and r.swe_agent_resolved is not True)
        ):
            workspace = task_output_dir / "swe_agent_workspace"
            prepare_workspace(task, workspace)
            logger.info("running SWE-agent for %s", task.id)
            swe = run_swe_agent(
                task,
                task_output_dir / "swe_agent",
                workspace,
                args.model,
                timeout_seconds=args.swe_agent_timeout,
                max_steps=args.swe_agent_max_steps,
                timeout_per_command=args.swe_agent_timeout_per_command,
            )
            r.swe_agent_resolved = swe["resolved"]
            r.swe_agent_duration = swe["duration"]
            r.swe_agent_error = swe["error"]
            logger.info("SWE-agent %s -> resolved=%s", task.id, r.swe_agent_resolved)
            if not r.swe_agent_resolved and is_infra_error(r.swe_agent_error):
                logger.error(
                    "SWE-agent infrastructure failure detected for %s: %s. "
                    "Aborting batch to avoid wasting time/token.",
                    task.id,
                    r.swe_agent_error,
                )
                break

        # Save incremental result.
        (task_output_dir / "comparison.json").write_text(
            json.dumps(asdict(r), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    report = {
        "metadata": {
            "started_at": datetime.utcnow().isoformat(),
            "mode": args.mode,
            "model": args.model,
            "task_count": len(tasks),
        },
        "tasks": [asdict(r) for r in results],
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(render_table(results), encoding="utf-8")
    logger.info("report saved to %s", output_dir)
    print(render_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
