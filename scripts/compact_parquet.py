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
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

TARGET_FILE_SIZE_BYTES = 128 * 1024 * 1024  # 128 MB


def _compute_percentile(sorted_values: list, p: float) -> float:
    """Compute the p-th percentile from a sorted list (linear interpolation)."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    if lo == hi:
        return float(sorted_values[lo])
    frac = idx - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def compute_significance_thresholds(combined: pa.Table) -> dict[str, dict[str, float]]:
    """Compute P95 significance thresholds from combined TAS event data.

    Returns a dict keyed by symbol (streamer symbol or underlying), each with:
      - p95_size: 95th percentile of raw trade sizes (abs(int))
      - p95_delta_weighted_size: 95th percentile of |size * delta|

    If delta is not available for a symbol, delta_weighted_size mirrors size.
    """
    # Filter to TAS events only
    import pyarrow.compute as pc
    tas_mask = pc.equal(combined.column("source_type"), "TIME_AND_SALE")
    tas = combined.filter(tas_mask)

    if tas.num_rows == 0:
        return {}

    # Safely extract columns (may be missing in older parquet files)
    col = tas.column
    symbols = col("symbol").to_pylist() if "symbol" in tas.column_names else [None] * tas.num_rows
    sizes = col("last_trade_size").to_pylist() if "last_trade_size" in tas.column_names else [None] * tas.num_rows
    deltas = col("delta").to_pylist() if "delta" in tas.column_names else [None] * tas.num_rows

    symbol_data: dict[str, list[tuple[int, float]]] = {}  # symbol -> pairs

    for sym, size, delta in zip(symbols, sizes, deltas, strict=True):
        if sym is None or size is None:
            continue
        size_int = int(size)
        delta_val = float(delta) if delta is not None else 0.0
        delta_weighted = size_int * abs(delta_val) if delta_val else size_int
        symbol_data.setdefault(sym, []).append((size_int, delta_weighted))

    thresholds: dict[str, dict[str, float]] = {}
    default_sizes: list[int] = []
    default_dw: list[float] = []

    for sym, pairs in symbol_data.items():
        raw_sizes = sorted([s for s, _ in pairs])
        dw_sizes = sorted([dw for _, dw in pairs])
        p95_size = _compute_percentile(raw_sizes, 95)
        p95_dw = _compute_percentile(dw_sizes, 95)
        thresholds[sym] = {
            "p95_size": round(p95_size, 2),
            "p95_delta_weighted_size": round(p95_dw, 2),
        }
        default_sizes.extend(raw_sizes)
        default_dw.extend(dw_sizes)

    # Also include a default (underlying-level) threshold
    if default_sizes:
        thresholds["default"] = {
            "p95_size": round(_compute_percentile(sorted(default_sizes), 95), 2),
            "p95_delta_weighted_size": round(_compute_percentile(sorted(default_dw), 95), 2),
        }

    return thresholds


def compact_date_partition(partition_dir: Path, target_size: int = TARGET_FILE_SIZE_BYTES) -> int:
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
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["compacted_files"] = written
    meta["compacted_at"] = dt.datetime.now(dt.UTC).isoformat()
    meta["row_count"] = combined.num_rows
    meta_path.write_text(json.dumps(meta, indent=2))

    # Compute and write significance thresholds
    thresholds = compute_significance_thresholds(combined)
    thresholds_path = partition_dir / "significance_meta.json"
    thresholds_output = {
        "date": partition_dir.name,
        "computed_at": meta["compacted_at"],
        "row_count": combined.num_rows,
        "symbols": thresholds,
    }
    thresholds_path.write_text(json.dumps(thresholds_output, indent=2))
    logger.info("Computed %d significance thresholds for %s", len(thresholds), thresholds_path.name)

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
