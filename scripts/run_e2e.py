#!/usr/bin/env python3
"""运行端到端测试并输出准确率报告。"""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/e2e/",
        "-v",
        "--tb=short",
    ]
    result = subprocess.run(cmd, cwd=root)

    # 使用 quiet 模式重新收集统计信息
    stat_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/e2e/",
        "-q",
        "--tb=no",
    ]
    stat_result = subprocess.run(stat_cmd, cwd=root, capture_output=True, text=True)
    last_line = stat_result.stdout.strip().splitlines()[-1] if stat_result.stdout else ""

    # 示例输出："8 passed in 0.60s" 或 "6 passed, 2 failed in 0.60s"
    passed = 0
    failed = 0
    parts = last_line.split()
    for i, part in enumerate(parts):
        if part == "passed":
            passed = int(parts[i - 1])
        elif part == "failed":
            failed = int(parts[i - 1])

    total = passed + failed
    accuracy = (passed / total * 100) if total > 0 else 0.0

    print("\n" + "=" * 60)
    print(f"E2E 测试总数: {total}")
    print(f"E2E 通过数:   {passed}")
    print(f"E2E 失败数:   {failed}")
    print(f"E2E 准确率:   {accuracy:.1f}%")
    print("=" * 60)

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
