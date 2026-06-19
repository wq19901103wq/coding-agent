"""Entry point: python -m swe_bench."""

from __future__ import annotations

import sys

from swe_bench.cli import main

if __name__ == "__main__":
    sys.exit(main())
