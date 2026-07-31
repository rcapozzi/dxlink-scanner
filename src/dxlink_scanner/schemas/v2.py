"""PyArrow schema v2 — extends v1 with Quote-derived columns.

Schema v2 extends v1 by adding top-level nullable columns for
mid_price, spread, spread_bps, and trade_vs_mid so that
derived Quote fields can be persisted alongside core event data.

Field removals from v1: `theo_price`, `underlying_price`, `delta`, `gamma`,
`dividend`, `interest` were dropped from the parquet schema (the corresponding
event source is no longer subscribed). `underlying_price` is now derived from
Quote `mid_price` at the application layer, not stored as a parquet column.
"""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

# Schema v2 — extends v1 with derived Quote columns
schema_v2 = pa.schema(
    [
        # Core fields (same as v1)
        ("event_id", pa.int64()),
        ("received_at", pa.string()),
        ("source_type", pa.string()),
        ("symbol", pa.string()),
        ("bid_price", pa.string()),
        ("ask_price", pa.string()),
        ("last_trade_price", pa.string()),
        ("last_trade_size", pa.int64()),
        ("last_trade_time", pa.int64()),
        ("last_trade_type", pa.string()),
        ("event_time_ms", pa.int64()),
        # Raw exchange timestamps
        ("time_ms", pa.int64()),
        ("time_nano_part_ms", pa.int64()),
        ("evict_at", pa.int64()),
        # New in v2: derived Quote fields (all nullable)
        ("mid_price", pa.string()),
        ("spread", pa.string()),
        ("spread_bps", pa.float64()),
        ("trade_vs_mid", pa.string()),
    ]
)

# List of new fields added in v2 (for migration)
v2_new_fields = [
    "mid_price",
    "spread",
    "spread_bps",
    "trade_vs_mid",
]

# V1 fields (for migration backfill)
v1_fields = [
    "event_id",
    "received_at",
    "source_type",
    "symbol",
    "bid_price",
    "ask_price",
    "last_trade_price",
    "last_trade_size",
    "last_trade_time",
    "last_trade_type",
    "event_time_ms",
    "time_ms",
    "time_nano_part_ms",
    "evict_at",
]
