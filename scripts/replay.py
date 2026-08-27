#!/usr/bin/env python
"""CLI wrapper for the replay framework.

Usage:
    uv run python scripts/replay.py --data-dir data/events --date 2024-07-31
    uv run python scripts/replay.py --data-dir data/events --all
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dxlink_scanner.replay.replay_engine import main  # noqa: E402

if __name__ == "__main__":
    main()
