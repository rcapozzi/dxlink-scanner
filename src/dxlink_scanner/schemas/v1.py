"""PyArrow schema for ConsolidatedEvent v1 parquet files."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

# Schema v1: matches ConsolidatedEvent fields + raw timestamp columns
# All fields are nullable to support partial messages and future evolution.
schema_v1 = pa.schema(
    [
        # Identity
        ("event_id", pa.int64()),
        ("received_at", pa.timestamp("ms", tz="UTC")),
        ("source_type", pa.string()),
        ("symbol", pa.string()),
        # Quote fields
        ("bid_price", pa.string()),  # Decimal as string for round-trip safety
        ("ask_price", pa.string()),
        # TimeAndSale fields
        ("last_trade_price", pa.string()),
        ("last_trade_size", pa.int64()),
        ("last_trade_time", pa.int64()),  # epoch ms
        ("last_trade_type", pa.string()),
        # TheoPrice / Greeks fields
        ("theo_price", pa.string()),
        ("underlying_price", pa.string()),
        ("delta", pa.string()),
        ("gamma", pa.string()),
        ("dividend", pa.string()),
        ("interest", pa.string()),
        # Raw timestamps (all epoch ms)
        ("event_time_ms", pa.int64()),
        ("time_ms", pa.int64()),
        ("time_nano_part_ms", pa.int64()),
        # Snapshot lifecycle
        ("evict_at", pa.int64()),
    ]
)
