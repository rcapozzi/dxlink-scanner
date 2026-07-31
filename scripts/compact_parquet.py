"""Nightly parquet compaction script.

Merges small parquet files within a date partition into larger files
to stay close to the 128MB target. Runs as a cron job or manually.

Usage:
    uv run python scripts/compact_parquet.py --data-dir data/events --date 2024-07-31
    uv run python scripts/compact_parquet.py --data-dir data/events --all
"""

from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

TARGET_FILE_SIZE_BYTES = 128 * 1024 * 1024  # 128 MB


def compact_date_partition(partition_dir: Path, target_size: int = TARGET_FILE_SIZE_BYTES) -> int:
    """Compact all parquet files in a date partition into fewer, larger files.

    Reads each day's files into a single combined table, then rewrites
    in target-size chunks. Returns number of files written.

    Args:
        partition_dir: Directory containing events_v1_*.parquet files for one date.
        target_size: Target file size in bytes.

    Returns:
        Number of output files written.
    """
    parquet_files = sorted(partition_dir.glob("events_v*_*.parquet"))
    if not parquet_files:
        logger.info("No parquet files found in %s", partition_dir)
        return 0

    logger.info("Compacting %d files in %s", len(parquet_files), partition_dir)

    # Read all files into a single Table
    tables = []
    for pf in parquet_files:
        table = pq.read_table(str(pf))
        tables.append(table)
    combined = pa.concat_tables(tables)
    logger.info("Combined %d rows across %d files", combined.num_rows, len(parquet_files))

    # Determine target row count per output file based on average row size
    total_bytes = combined.nbytes
    avg_row_size = total_bytes / combined.num_rows if combined.num_rows else 1
    rows_per_file = max(1, int(target_size / avg_row_size))

    # Write compacted files
    written = 0
    schema = combined.schema
    total_rows = combined.num_rows
    offset = 0
    while offset < total_rows:
        end = min(offset + rows_per_file, total_rows)
        chunk = combined.slice(offset, end - offset)
        fname = f"events_v1_compact_{int(dt.datetime.now().timestamp())}_{written}.parquet"
        out_path = partition_dir / fname
        pq.write_table(chunk, str(out_path), compression="zstd")
        written += 1
        logger.info("Wrote %d/%d rows to %s", end, total_rows, out_path.name)
        offset = end

    # Remove original files
    for pf in parquet_files:
        pf.unlink()
    logger.info("Removed %d original files", len(parquet_files))

    # Update session_meta.json
    meta_path = partition_dir / "session_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["compacted_files"] = written
        meta["compacted_at"] = dt.datetime.now(dt.UTC).isoformat()
        meta_path.write_text(json.dumps(meta, indent=2))

    # Clean up
    gc.collect()

    return written


def find_date_partitions(data_dir: Path) -> list[Path]:
    """Find all date-partitioned directories."""
    partitions = []
    for d in sorted(data_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            try:
                dt.date.fromisoformat(d.name)
                partitions.append(d)
            except ValueError:
                continue
    return partitions


def main() -> None:
    parser = argparse.ArgumentParser(description="Compact parquet event files nightly")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/events"),
        help="Base data directory (default: data/events)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Specific date to compact (YYYY-MM-DD). If omitted with --all, compacts yesterday.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Compact all date partitions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be compacted without writing",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    data_dir = args.data_dir
    if not data_dir.exists():
        logger.error("Data directory does not exist: %s", data_dir)
        sys.exit(1)

    if args.all:
        targets = find_date_partitions(data_dir)
    elif args.date:
        targets = [data_dir / args.date]
    else:
        targets = [data_dir / (dt.date.today() - dt.timedelta(days=1)).isoformat()]

    for partition in targets:
        if not partition.exists():
            logger.warning("Partition does not exist: %s", partition)
            continue
        if args.dry_run:
            files = list(partition.glob("events_v*_*.parquet"))
            logger.info("[DRY RUN] Would compact %d files in %s", len(files), partition)
        else:
            n = compact_date_partition(partition)
            logger.info("Compact %s: %d files written", partition.name, n)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
