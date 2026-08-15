#!/usr/bin/env python
"""CLI wrapper for the benchmark suite.

Usage:
    uv run python scripts/benchmark.py --iterations 10000
    uv run python scripts/benchmark.py --iterations 10000 --output results/benchmark.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dxlink_scanner.benchmark.perf import main  # noqa: E402

if __name__ == "__main__":
    main()
