# Consolidated DXFeed Event Model — Design Specification

## Executive Summary

The scanner processes `Quote` and `TimeAndSale` as independent event streams. A **consolidated event model** merges Quote into a single per-symbol snapshot for cross-message analytics (e.g., spread, mid_price). TimeAndSale events flow directly to the rule engine without merging into the snapshot.

This spec documents the **current implemented state** — not a future plan.

---

## 1. Why Consolidate?

| Use Case | Separate Streams | Consolidated |
|----------|------------------|--------------|
| **Spread analysis** | Manual correlation by timestamp | `quote.bid/ask` vs `mid_price` in one snapshot |
| **Alerting** | Rules see only TAS size | Rules can factor `bid/ask`, `mid_price`, spread |
| **Backtesting** | Two streams to join | Single parquet with Quote state per symbol |

**Bottom line**: Downstream logic (rules, ML, dashboards) needs Quote fields for the same symbol — consolidation pays off. TimeAndSale events are handled separately by the rule engine.

---

## 2. Consolidated Data Model

### 2.1 Per-Symbol State (Latest Snapshot)

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

@dataclass(slots=True)
class ConsolidatedSnapshot:
    # Identity
    symbol: str                    # DXLink streamer symbol (e.g., "SPY250731C00450000")
    underlying_symbol: str         # e.g., "SPY" or "/ES:XCME"
    updated_at: datetime           # Wall-clock of last update (UTC)

    # Quote (best bid/ask) — from Quote event
    bid_price: Optional[Decimal] = None
    ask_price: Optional[Decimal] = None

    # Derived / computed (filled by enrichment pipeline)
    mid_price: Optional[Decimal] = None          # (bid + ask) / 2
    spread: Optional[Decimal] = None             # ask - bid
    spread_bps: Optional[float] = None           # spread / mid * 10000

    # Lifecycle
    evict_at: Optional[int] = None               # epoch ms (for daily expiry eviction)
```

**Note**: `underlying_price` is **not** on the snapshot. It is derived at alert time from the underlying's Quote `mid_price` (`(bid + ask) / 2`).

TimeAndSale (last trade) is **not** on the snapshot. TAS events go directly to the rule engine.

### 2.2 Immutable Event Log (Append-Only)

```python
@dataclass(frozen=True, slots=True)
class ConsolidatedEvent:
    # One row per incoming DXLink message
    event_id: int                    # Monotonic counter
    received_at: datetime            # When we received it
    source_type: Literal["QUOTE", "TIME_AND_SALE"]
    symbol: str
    # Only the fields present in this message type are non-None
    bid_price: Optional[Decimal] = None
    ask_price: Optional[Decimal] = None
    event_time_ms: Optional[int] = None        # raw DXLink event timestamp
```

**Storage**: `ConsolidatedEvent` → append to parquet (partitioned by date). `ConsolidatedSnapshot` → in-memory dict for real-time access.

---

## 3. Storage Strategy

| Requirement | Implementation | Rationale |
|-------------|----------------|-----------|
| **Real-time snapshot** (latest state per symbol) | `dict[str, ConsolidatedSnapshot]` | O(1) lookup, ~10K symbols max, < 5 MB |
| **Historical replay / backtest** | Parquet files (partitioned by `date`) | Columnar, compression, predicate pushdown |
| **Rolling window stats** (median, MAD, etc.) | `RollingStatsManagerV2` + snapshot fields | Already implemented |
| **Cross-symbol analytics** (IV surface, term structure) | Query parquet with DuckDB / Polars | OLAP on columnar data |

### 3.1 In-Memory: Global Dict

```python
# Simple, thread-safe for single-threaded asyncio loop
snapshot_store: dict[str, ConsolidatedSnapshot] = {}

async def on_message(event: ConsolidatedEvent):
    snap = snapshot_store.get(event.symbol)
    if snap is None:
        snap = ConsolidatedSnapshot(symbol=event.symbol, underlying_symbol=resolve_underlying(event.symbol))
    merge_into_snapshot(snap, event)
    snapshot_store[event.symbol] = snap
```

**No Pandas DataFrame** for real-time — dict of dataclasses is faster, type-safe, and avoids DataFrame mutation overhead.

### 3.2 Persistence: Parquet + DuckDB

```python
# Batch write every N events or every T seconds
async def flush_events(event_buffer: list[ConsolidatedEvent]):
    df = pl.DataFrame([asdict(e) for e in event_buffer])
    df.write_parquet(f"data/events/{date.today()}/events_{uuid4()}.parquet")
```

**Partitioning**: `data/events/yyyy-mm-dd/events_<session_id>_<timestamp>.parquet`

---

## 4. Integration Points

### 4.1 In `cli.py` — Unified Consumer

```python
# Two producers → bounded asyncio.Queue → single consumer
async def _run_scanner():
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=config.stream.backpressure_queue_size)
    
    async def _produce_quotes():
        async for quote in streamer.listen_quotes(symbols):
            await queue.put(("QUOTE", quote))
    
    async def _produce_tas():
        async for tas in streamer.listen_time_and_sale(symbols):
            await queue.put(("TAS", tas))
    
    async def _consume():
        while True:
            event_type, raw = await queue.get()
            event = normalize(raw, event_type)
            await store.ingest(event)
    
    await asyncio.gather(_produce_quotes(), _produce_tas(), _consume())
```

### 4.2 Rule Engine Enhancement

```python
# Rules can now access snapshot fields:
def process(self, event: TimeAndSaleEvent) -> Alert | None:
    snap = self.snapshot_store.get(event.symbol)
    if snap and snap.mid_price:
        # Spread-aware anomaly: large print vs mid
        edge = event.price - snap.mid_price
        ...
    
    # Underlying price from Quote mid_price on underlying
    if snap and snap.underlying_symbol:
        underlying_snap = self.snapshot_store.get(snap.underlying_symbol)
        if underlying_snap and underlying_snap.mid_price:
            alert.underlying_price = float(underlying_snap.mid_price)
```

---

## 5. Open Items (Active)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 57 | **Graceful shutdown** — At 17:00 ET, scanner exits. Need signal handler (SIGTERM/SIGINT) to flush parquet, close DXLink cleanly. | 🔴 Open | Add to `cli.py` `_run_scanner` or `SnapshotStore`. Test with `kill -TERM`. |
| 58 | **Pydantic validation details** — `Field(gt=0)` on new config fields. What about upper bounds (e.g., `backpressure_queue_size < 100000`)? | 🔴 Open | Add `Field(gt=0, le=100000)`? Or custom validator with descriptive error. |
| 59 | **DXLink reconnection post-MVP** — Process dies on error for MVP. Post-MVP: implement exponential backoff + resubscribe. | 🟡 Design | In `SnapshotStore` or new `DXLinkManager` wrapper. |
| 60 | **Dynamic strike management** — Periodic chain re-scan (60 min) to catch new 0DTE strikes. How to add/remove subscriptions on live streamer? | 🟡 Design | `streamer.subscribe()` / `streamer.unsubscribe()` on the fly. Track active symbols. |
| 61 | **Parquet compaction job** — Nightly job to rewrite daily files. Where to run? Separate script? Cron? | 🟡 Plan | `scripts/compact_parquet.py` + cron at 02:00 ET. Read prior day, rewrite single file. |
| 62 | **Index options Quote format** — Quote works on SPX/NDX/RUT without exchange suffix. Confirm streamer symbol format (`SPX250731C00450000` vs `/SPX:XCBO`). | 🔴 Open | Test and document. May differ from equity/futures. |
| 63 | **Schema v2 design** — Top-level nullable columns for derived Quote fields (`mid_price`, `spread`, etc.). When to add? Post-MVP milestone. | 🟡 Plan | Add to `schemas/v2.py`. Migration script v1→v2. |
| 64 | **ConsolidatedSnapshot `evict_at` type** — Currently `int` (epoch ms). What about timezone-aware `datetime` for readability in logs? | 🟡 Design | Keep `int` for logic; add `evict_at_dt` property returning `datetime` in UTC. |

---

## 6. Implementation Status

| Component | Status | Location |
|-----------|--------|----------|
| `ConsolidatedSnapshot` + `merge_into_snapshot()` | ✅ Done | `src/dxlink_scanner/models.py` |
| `SnapshotStore` class | ✅ Done | `src/dxlink_scanner/snapshot_store.py` |
| Single consumer loop (2 producers → Queue → consumer) | ✅ Done | `src/dxlink_scanner/cli.py` |
| `CELRuleEngine` uses snapshot store | ✅ Done | `src/dxlink_scanner/rules/cel_engine.py` |
| Parquet batch writer (background task) | ✅ Done | `src/dxlink_scanner/snapshot_store.py` |
| Config flags (`persist_events`, `backpressure_queue_size`, `flush_interval_sec`, `flush_batch_size`) | ✅ Done | `src/dxlink_scanner/config.py` |
| Parquet schema v1 | ✅ Done | `src/dxlink_scanner/schemas/v1.py` |
| Parquet schema v2 (derived Quote columns) | ✅ Done | `src/dxlink_scanner/schemas/v2.py` |
| Underlying price from Quote mid_price | ✅ Done | `cel_engine.py` + `models.py` |

---

## 7. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `polars` | ≥1.0 | Fast DataFrame → parquet |
| `pyarrow` | ≥15 | Parquet schema, partitioning |
| `duckdb` | ≥1.0 | Ad-hoc analytics on parquet (optional) |
| `prometheus-client` | ≥0.19 | Metrics exposition (post-MVP) |

All are light, pure-Python/Arrow, no heavy ML deps.

---

## 8. Next Steps

1. **Implement graceful shutdown** (signal handler → flush parquet → close DXLink)
2. **Add Pydantic bounds validation** on new config fields
3. **Test index options Quote format** (SPX/NDX/RUT)
4. **Design parquet compaction job** (separate script + cron)
5. **Design DXLink reconnection logic** (post-MVP)

---

*This spec reflects the current implemented state. Update as open items are resolved.*