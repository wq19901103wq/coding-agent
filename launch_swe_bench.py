#!/usr/bin/env python3
"""Launch the SWE-bench benchmark in a background subprocess."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    output_dir = Path("output/swe-lite-full")
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "run.log"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    log_file = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "swe_bench.cli",
            "--dataset",
            "data/swe-bench-lite-test.json",
            "--output",
            str(output_dir),
            "--timeout",
            "900",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=Path(__file__).resolve().parent,
        start_new_session=True,
    )

    pid_path = output_dir / "run.pid"
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    print(f"started swe-bench benchmark pid={proc.pid}, log={log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
