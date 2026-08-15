"""PyArrow schema v2 — extends v1 with Quote-derived columns.

Schema v2 extends v1 by adding top-level nullable columns for
mid_price, spread, spread_bps, and trade_vs_mid so that
derived Quote fields can be persisted alongside core event data.

Includes TheoPrice/Greeks fields from v1, plus v2 microstructure fields:
VAP profile, liquidity metrics, VPIN, trade classification, and cross-asset
flow metrics.
"""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

# Schema v2 — extends v1 with derived Quote columns and microstructure fields
schema_v2 = pa.schema(
    [
        # Core fields (same as v1)
        ("event_id", pa.int64()),
        ("received_at", pa.string()),
        ("source_type", pa.string()),
        ("symbol", pa.string()),
        ("bid_price", pa.string()),  # Decimal as string for round-trip safety
        ("ask_price", pa.string()),
        # TimeAndSale fields
        ("last_trade_price", pa.string()),
        ("last_trade_size", pa.int64()),
        ("last_trade_time", pa.int64()),  # epoch ms
        ("last_trade_type", pa.string()),
        ("event_time_ms", pa.int64()),
        # Raw exchange timestamps
        ("time_ms", pa.int64()),
        ("time_nano_part_ms", pa.int64()),
        ("evict_at", pa.int64()),
        # TheoPrice / Greeks fields (from v1)
        ("theo_price", pa.string()),
        ("underlying_price", pa.string()),
        ("delta", pa.string()),
        ("gamma", pa.string()),
        ("dividend", pa.string()),
        ("interest", pa.string()),
        # New in v2: derived Quote fields (all nullable)
        ("mid_price", pa.string()),
        ("spread", pa.string()),
        ("spread_bps", pa.float64()),
        ("trade_vs_mid", pa.string()),
        # New in v2: microstructure fields (all nullable)
        ("vap_poc", pa.string()),
        ("vap_val_area_low", pa.string()),
        ("vap_val_area_high", pa.string()),
        ("vap_imbalance", pa.float64()),
        ("spread_p50", pa.float64()),
        ("spread_p95", pa.float64()),
        ("depth_at_poc_median", pa.float64()),
        ("vpin", pa.float64()),
        ("trade_side", pa.string()),
        ("cross_asset_vpin", pa.float64()),
        ("systemic_score", pa.float64()),
    ]
)

# List of new fields added in v2 (for migration)
v2_new_fields = [
    "mid_price",
    "spread",
    "spread_bps",
    "trade_vs_mid",
    "vap_poc",
    "vap_val_area_low",
    "vap_val_area_high",
    "vap_imbalance",
    "spread_p50",
    "spread_p95",
    "depth_at_poc_median",
    "vpin",
    "trade_side",
    "cross_asset_vpin",
    "systemic_score",
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
    "theo_price",
    "underlying_price",
    "delta",
    "gamma",
    "dividend",
    "interest",
    # Microstructure fields added in v2 schema extension
    "vap_poc",
    "vap_val_area_low",
    "vap_val_area_high",
    "vap_imbalance",
    "spread_p50",
    "spread_p95",
    "depth_at_poc_median",
    "vpin",
    "trade_side",
    "cross_asset_vpin",
    "systemic_score",
]
