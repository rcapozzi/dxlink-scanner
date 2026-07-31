# Data Files & Storage

Complete reference for the scanner's Parquet storage, schemas, data formats, and file organization.

## Storage Architecture

```
data/
└── events/
    ├── 2024-07-30/
    │   ├── events_v1_<session_id>_<timestamp>.parquet
    │   ├── events_v1_<session_id>_<timestamp>.parquet
    │   └── session_meta.json
    ├── 2024-07-31/
    │   ├── events_v1_<session_id>_<timestamp>.parquet
    │   └── session_meta.json
    └── ...
```

**Partitioning**: By date only (`YYYY-MM-DD`). Symbol is a column for predicate pushdown (not Hive-partitioned to avoid many small files).

## Parquet Schemas

### Schema v1 (`src/dxlink_scanner/schemas/v1.py`)

Core schema for `ConsolidatedEvent` — Quote + TimeAndSale fields, all nullable for forward compatibility.

```python
schema_v1 = pa.schema([
    # Identity
    ("event_id", pa.int64()),
    ("received_at", pa.timestamp("ms", tz="UTC")),
    ("source_type", pa.string()),          # "QUOTE" | "TIME_AND_SALE"
    ("symbol", pa.string()),               # DXLink streamer symbol

    # Quote fields — from Quote event
    ("bid_price", pa.string()),            # Decimal as string
    ("ask_price", pa.string()),
    ("bid_size", pa.int64()),
    ("ask_size", pa.int64()),
    ("bid_time", pa.timestamp("ms", tz="UTC")),
    ("ask_time", pa.timestamp("ms", tz="UTC")),

    # TimeAndSale fields — from TAS event
    ("last_trade_price", pa.string()),
    ("last_trade_size", pa.int64()),
    ("last_trade_time", pa.int64()),       # epoch ms
    ("last_trade_exchange", pa.string()),
    ("last_trade_type", pa.string()),

    # Raw exchange timestamps (all epoch ms)
    ("event_time_ms", pa.int64()),
    ("time_ms", pa.int64()),
    ("time_nano_part_ms", pa.int64()),
    ("bid_time_ms", pa.int64()),
    ("ask_time_ms", pa.int64()),

    # Snapshot lifecycle
    ("evict_at", pa.int64()),              # epoch ms for TTL
])
```

**Design Decisions**:
| Decision | Rationale |
|----------|-----------|
| Decimal → string | Round-trip precision; avoids float encoding issues |
| Timestamps → epoch ms (int64) | Timezone-agnostic, fast comparison, easy DuckDB cast |
| All fields nullable | Partial messages (Quote has no trade fields, TAS has no bid/ask) |
| Date-only partitioning | Symbol partitioning creates too many files (10K symbols = 10K dirs) |

### Schema v2 (`src/dxlink_scanner/schemas/v2.py`)

Extends v1 with derived Quote columns for analytics.

```python
# New in v2: derived Quote fields (all nullable)
("mid_price", pa.string()),
("spread", pa.string()),
("spread_bps", pa.float64()),
("trade_vs_mid", pa.string()),
```

**Migration**: Existing v1 files remain readable. Migration script can backfill v1 → v2 by adding new columns as null.

### Schema Versioning

| Version | File Prefix | Description |
|---------|-------------|-------------|
| v1 | `events_v1_...` | Core consolidated events |
| v2 | `v2_...` | + derived Quote columns (mid_price, spread, spread_bps, trade_vs_mid) |

Version encoded in filename for easy identification.

## Session Metadata

Each date partition contains `session_meta.json`:

```json
{
  "session_id": "018f4a2b-3c5d-7e8f-9a0b-c1d2e3f4a5b6",
  "schema_version": "v1",
  "event_count": 123456,
  "created_at": "2024-07-31T16:00:00+00:00",
  "files": [
    "events_v1_018f4a2b-3c5d-7e8f-9a0b-c1d2e3f4a5b6_1722441600.parquet",
    "events_v1_018f4a2b-3c5d-7e8f-9a0b-c1d2e3f4a5b6_1722445200.parquet"
  ],
  "compacted_files": 1,
  "compacted_at": "2024-08-01T02:00:00+00:00"
}
```

| Field | Description |
|-------|-------------|
| `session_id` | UUID v7 (time-ordered, sortable) |
| `schema_version` | `v1` or `v2` |
| `event_count` | Total events written this session |
| `files` | List of parquet files in this session |
| `compacted_files` | Files written by compaction job |
| `compacted_at` | Timestamp of last compaction |

## File Naming Convention

```
events_v{version}_{session_id}_{unix_timestamp}.parquet
```

Example: `events_v1_018f4a2b-3c5d-7e8f-9a0b-c1d2e3f4a5b6_1722441600.parquet`

| Component | Format | Purpose |
|-----------|--------|---------|
| `events` | literal | File type identifier |
| `v{version}` | `v1`, `v2` | Schema version |
| `session_id` | UUID v7 | Unique session identifier |
| `unix_timestamp` | epoch seconds | File creation time (sortable) |

## Data Types & Encoding

### Decimal Fields
Stored as **UTF-8 strings** in parquet:
- `bid_price`, `ask_price`, `last_trade_price`, `mid_price` (v2)
- Encoding: `"451.25"`, `"2.50"`, `"0.01"`

**Reading with DuckDB**:
```sql
SELECT symbol, CAST(bid_price AS DECIMAL(18,4)) AS bid_price
FROM read_parquet('data/events/2024-07-31/*.parquet')
WHERE source_type = 'QUOTE';
```

### Timestamp Fields
Two representations:

| Field | Type | Description |
|-------|------|-------------|
| `received_at` | `timestamp[ms, tz=UTC]` | When scanner received the event |
| `event_time_ms`, `time_ms`, `time_nano_part_ms`, `bid_time_ms`, `ask_time_ms`, `last_trade_time` | `int64` | Raw exchange timestamps (epoch ms) |

**Reading with DuckDB**:
```sql
SELECT 
  symbol,
  received_at,
  to_timestamp(event_time_ms / 1000) AS exchange_time
FROM read_parquet('data/events/2024-07-31/*.parquet');
```

### Source Type Values

| Value | Description | Typical Fields Present |
|-------|-------------|------------------------|
| `QUOTE` | Best bid/ask | bid/ask price/size, bid/ask time |
| `TIME_AND_SALE` | Trade print | last_trade_price, size, trade_type, exchange |

## Writing Parquet (SnapshotStore)

### Flush Triggers
```python
# In SnapshotStore:
if len(self._buffer) >= self._flush_batch_size:    # 10,000 events
    await self.flush()
# OR background task:
await asyncio.sleep(self._flush_interval_sec)      # 5 seconds
```

### Write Process
```python
async def _flush_to_parquet(self, output_dir: Path):
    batch = self._buffer[:]
    self._buffer.clear()
    
    # Convert to schema-aligned dicts
    rows = []
    for ev in batch:
        d = {}
        for field in schema_v1.names:
            v = getattr(ev, field, None)
            if isinstance(v, Decimal):
                d[field] = str(v)
            else:
                d[field] = v
        rows.append(d)
    
    table = pa.Table.from_pylist(rows, schema=schema_v1)
    
    today = dt.date.today()
    out_dir = output_dir / str(today)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    fname = f"events_v1_{self._session_id}_{int(dt.datetime.now().timestamp())}.parquet"
    pq.write_table(table, str(out_path))
```

### Compression
Default: `zstd` (via `pq.write_table(..., compression="zstd")`)
- Good compression ratio for financial data
- Fast decompression for analytics

## Reading Parquet

### With Polars (Fast Analytics)
```python
import polars as pl

# Read all files for a date
df = pl.read_parquet("data/events/2024-07-31/*.parquet")

# Filter & aggregate
trades = df.filter(pl.col("source_type") == "TIME_AND_SALE")
print(trades.groupby("symbol").agg(
    pl.col("last_trade_size").sum().alias("total_volume"),
    pl.col("last_trade_price").mean().alias("avg_price"),
))
```

### With DuckDB (SQL Analytics)
```python
import duckdb

conn = duckdb.connect()
conn.execute("""
    CREATE VIEW events AS 
    SELECT * FROM read_parquet('data/events/2024-07-31/*.parquet')
""")

-- Spread analysis (Quote)
SELECT symbol, 
       AVG(CAST(ask_price AS DOUBLE) - CAST(bid_price AS DOUBLE)) as avg_spread,
       AVG((CAST(ask_price AS DOUBLE) - CAST(bid_price AS DOUBLE)) / 
           ((CAST(ask_price AS DOUBLE) + CAST(bid_price AS DOUBLE)) / 2) * 10000) as avg_spread_bps
FROM events
WHERE source_type = 'QUOTE' AND bid_price IS NOT NULL
GROUP BY symbol;
```

### With PyArrow (Schema Inspection)
```python
import pyarrow.parquet as pq

pf = pq.ParquetFile("data/events/2024-07-31/events_v1_....parquet")
print(pf.schema)              # Full schema
print(pf.metadata.num_rows)   # Row count
print(pf.metadata.num_row_groups)
```

## Compaction Job (`scripts/compact_parquet.py`)

Merges small daily files into larger ~128MB files for query performance.

### Usage
```bash
# Compact specific date
uv run python scripts/compact_parquet.py --data-dir data/events --date 2024-07-31

# Compact all dates
uv run python scripts/compact_parquet.py --data-dir data/events --all

# Dry run (show what would happen)
uv run python scripts/compact_parquet.py --data-dir data/events --date 2024-07-31 --dry-run
```

### Process
1. Read all parquet files in date partition
2. Concatenate into single PyArrow Table
3. Calculate target row count per file (128MB target)
4. Slice and write compacted files with `zstd` compression
5. Remove original files
6. Update `session_meta.json` with compaction info

### Scheduling (Cron)
```bash
# Run at 02:00 ET daily
0 2 * * * cd /opt/scanner && uv run python scripts/compact_parquet.py --data-dir data/events --all >> /var/log/scanner_compact.log 2>&1
```

## Event Lifecycle & TTL

### `evict_at` Field
- **Type**: `int64` (epoch ms)
- **Purpose**: Snapshot eviction timestamp
- **Population**: At chain load, `expiry_date_midnight_ms + 1_hour_buffer`

### Eviction Logic
```python
# In SnapshotStore (run periodically)
now = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
for symbol, evict_at in list(self._evict_map.items()):
    if now >= evict_at:
        self._snapshots.pop(symbol, None)
        self._evict_map.pop(symbol, None)
```

### Daily Restart
Scanner restarts 17:00–18:00 ET (market close):
1. SIGTERM received → `store.flush_remaining()`
2. Process exits
3. Scheduler restarts scanner
4. New session loads prior day's parquet for rolling stats warm-up

## Data Volume Estimates

| Metric | Estimate |
|--------|----------|
| Events/day (3 underlyings, 0DTE) | ~500K - 2M |
| Parquet file size (10K events) | ~5-15 MB |
| Daily total (uncompacted) | ~50-300 MB |
| Daily total (compacted) | ~1-3 files × 128 MB |
| 30-day retention | ~3-10 GB |

## Query Patterns

### Typical Analytical Queries

```sql
-- Volume by symbol (TAS only)
SELECT symbol, COUNT(*) as trades, SUM(last_trade_size) as volume
FROM events
WHERE source_type = 'TIME_AND_SALE'
GROUP BY symbol
ORDER BY volume DESC;

-- Spread analysis (Quote)
SELECT symbol, 
       AVG(CAST(ask_price AS DOUBLE) - CAST(bid_price AS DOUBLE)) as avg_spread,
       AVG((CAST(ask_price AS DOUBLE) - CAST(bid_price AS DOUBLE)) / 
           ((CAST(ask_price AS DOUBLE) + CAST(bid_price AS DOUBLE)) / 2) * 10000) as avg_spread_bps
FROM events
WHERE source_type = 'QUOTE' AND bid_price IS NOT NULL
GROUP BY symbol;

-- Trade vs mid deviation
SELECT symbol,
       AVG(CAST(last_trade_price AS DOUBLE) - 
           (CAST(bid_price AS DOUBLE) + CAST(ask_price AS DOUBLE)) / 2) as avg_trade_vs_mid
FROM events e1
WHERE source_type = 'TIME_AND_SALE' AND last_trade_price IS NOT NULL
  AND symbol IN (SELECT symbol FROM events WHERE source_type = 'QUOTE')
GROUP BY symbol;

-- Anomaly detection backtest
SELECT symbol, received_at, last_trade_size,
       last_trade_size / LAG(last_trade_size) OVER (PARTITION BY symbol ORDER BY received_at) as size_ratio
FROM events
WHERE source_type = 'TIME_AND_SALE'
  AND last_trade_size > 100
ORDER BY received_at DESC
LIMIT 1000;
```

## Backup & Retention

| Policy | Recommendation |
|--------|----------------|
| Raw parquet | Retain 30-90 days (compressed ~10 GB/30 days) |
| Compacted parquet | Retain 1+ years for backtesting |
| Session metadata | Retain indefinitely (tiny) |
| Backup | S3/GS sync of `data/events/` daily |

## Troubleshooting Data Files

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| Many small files | Flush interval too low or high event rate | Increase `flush_batch_size` or `flush_interval_sec` |
| Schema mismatch | Mixed v1/v2 files | Run migration script or filter by filename prefix |
| Missing `evict_at` | Legacy files | Parse expiry from symbol string on load |
| Corrupted file | `pq.read_table` fails | Check `pq.ParquetFile().metadata` for corruption |
| High memory reading | Large date partition | Use DuckDB/predicate pushdown instead of full load |

## Schema Evolution Guidelines

1. **Never remove columns** — add new columns at end
2. **Always nullable** — `nullable=True` for new columns
3. **Version in filename** — `events_v{N}_...` prefix
4. **Document changes** — Update `schemas/v{N}.py` with comments
5. **Test round-trip** — Write → read → verify in test suite