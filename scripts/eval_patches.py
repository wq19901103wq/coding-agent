#!/usr/bin/env python
"""Evaluate patches from mini-swe-agent and claude code using DockerEvaluator."""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from swe_bench.dataset import SWEBenchDataset
from swe_bench.docker import DockerEvaluator

load_dotenv(override=True)

DATASET = "data/swe-bench-lite-test.json"


def evaluate_batch(output_dir: str, batch_name: str):
    """Evaluate all patches in output_dir using DockerEvaluator."""
    ds = SWEBenchDataset(DATASET)
    all_tasks = {t.id: t for t in ds.list_tasks()}

    out = Path(output_dir)
    results = []
    for case_dir in sorted(out.iterdir()):
        if not case_dir.is_dir():
            continue
        patch_path = case_dir / "agent.patch"
        if not patch_path.exists():
            continue
        iid = case_dir.name
        if iid not in all_tasks:
            continue
        task = all_tasks[iid]
        patch = patch_path.read_text(encoding="utf-8", errors="replace")
        if not patch.strip():
            results.append({"instance_id": iid, "resolved": False, "error": "empty patch"})
            continue

        # Resume check
        eval_path = case_dir / "eval_result.json"
        if eval_path.exists():
            results.append(json.loads(eval_path.read_text()))
            continue

        ws = case_dir / "workspace"
        print(f"evaluating {iid}...")
        try:
            ev = DockerEvaluator(task, timeout_seconds=600, output_dir=out)
            result = ev.evaluate(patch, ws if ws.exists() else None)
            r = {
                "instance_id": iid,
                "resolved": result.resolved,
                "success": result.success,
                "error": (result.error or "")[:80],
            }
            eval_path.write_text(json.dumps(r))
            results.append(r)
            print(f"  {iid}: resolved={result.resolved}")
        except Exception as e:
            r = {"instance_id": iid, "resolved": False, "error": str(e)[:80]}
            eval_path.write_text(json.dumps(r))
            results.append(r)
            print(f"  {iid}: ERROR {e}")

    resolved = sum(1 for r in results if r.get("resolved"))
    total = len(results)
    percentage = resolved / total * 100 if total else 0.0
    print(f"\n=== {batch_name}: {resolved}/{total} resolved ({percentage:.1f}%) ===")
    return results


if __name__ == "__main__":
    batch = sys.argv[1] if len(sys.argv) > 1 else "all"
    if batch in ("claude", "all"):
        evaluate_batch("output/claude-code-swe-24", "Claude Code")
    if batch in ("mini", "all"):
        evaluate_batch("output/mini-swe-ds-flash-24", "mini-SWE-agent")
