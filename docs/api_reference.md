# Python API Reference

Complete reference for the scanner's public Python API.

## Module Index

| Module | Description |
|--------|-------------|
|| `dxlink_scanner.config` | Configuration loading & Pydantic models |
|| `dxlink_scanner.models` | Core data models (Alert, events, snapshots) |
|| `dxlink_scanner.rules.cel_engine` | CEL-based rule engine |
| `dxlink_scanner.rules` | Rule engine exports |
| `dxlink_scanner.sinks.stdout_sink` | JSON lines output sink |
| `dxlink_scanner.sinks.webhook_sink` | HTTP webhook sink |
| `dxlink_scanner.sinks` | Sink exports |
| `dxlink_scanner.stats` | Rolling statistics manager (V2) |
| `dxlink_scanner.snapshot_store` | In-memory snapshot + Parquet persistence |
| `dxlink_scanner.schemas.v1` | Parquet schema v1 |
| `dxlink_scanner.schemas.v2` | Parquet schema v2 |
| `dxlink_scanner.cli` | CLI entry point |

---

## `dxlink_scanner.config`

### `load_config(config_path: str | Path) -> ScannerConfig`

Load and validate configuration from YAML file with environment variable substitution.

```python
from dxlink_scanner.config import load_config
from pathlib import Path

config = load_config("production.yaml")
# or
config = load_config(Path("/etc/dxlink-scanner/production.yaml"))
```

**Parameters**:
- `config_path`: Path to YAML config file

**Returns**: `ScannerConfig` instance

**Raises**:
- `FileNotFoundError`: Config file not found
- `ValidationError`: Config doesn't match schema

### Configuration Models

#### `ScannerConfig`
Top-level configuration container.

| Field | Type | Default |
|-------|------|---------|
| `tastytrade` | `TastyTradeConfig` | Required |
| `watchlist` | `WatchlistConfig` | `WatchlistConfig()` |
| `detection` | `DetectionConfig` | `DetectionConfig()` |
| `outputs` | `OutputsConfig` | `OutputsConfig()` |
| `logging` | `LoggingConfig` | `LoggingConfig()` |
| `stream` | `StreamConfig` | `StreamConfig()` |

#### `TastyTradeConfig`
```python
class TastyTradeConfig(BaseModel):
    client_id: str
    client_secret: str
    refresh_token: str
    sandbox: bool = False
```

#### `TickerConfig`
```python
class TickerConfig(BaseModel):
    symbol: str
    option_type: str = "equity"           # "equity" | "futures"
    strikes_around_atm: int = 10          # 1-50
    expiration_filter: str = "0DTE"       # "0DTE" | "all"
    alert_rules: list[CelAlertRule] = []
    underlying_alert_rules: list[CelAlertRule] = []

#### `WatchlistConfig`
```python
class WatchlistConfig(BaseModel):
    tickers: list[TickerConfig] = [TickerConfig(symbol="SPY")]
    default_alert_rules: list[CelAlertRule] = []
    
    @property
    def symbols(self) -> list[str]:
        return [t.symbol for t in self.tickers]
```

#### `DetectionConfig`
```python
class DetectionConfig(BaseModel):
    size_mult: float = 5.0        # > 0
    abs_min_size: int = 10        # >= 1
    stats_window: int = 50        # 10-500
```

#### `CelAlertRule`
```python
class CelAlertRule(BaseModel):
    name: str
    expression: str
    severity: str = "high"        # info|low|medium|high|critical
```

#### `WebhookConfig`
```python
class WebhookConfig(BaseModel):
    enabled: bool = False
    url: str | None = None
    timeout_seconds: float = 5.0
    max_retries: int = 3
```

#### `OutputsConfig`
```python
class OutputsConfig(BaseModel):
    stdout: bool = True
    webhook: WebhookConfig = WebhookConfig()
    data_dir: str = "data/events"
    persist_events: bool = False
```

#### `StreamConfig`
```python
class StreamConfig(BaseModel):
    backpressure_queue_size: int = 500      # 1-100000
    flush_interval_sec: float = 5.0
    flush_batch_size: int = 10000
```

---

## `dxlink_scanner.models`

### Core Event Models

#### `TimeAndSaleEvent`
```python
@dataclass(frozen=True, slots=True)
class TimeAndSaleEvent:
    symbol: str
    price: Decimal
    size: int
    timestamp: datetime
    event_type: Literal["TimeAndSale"] = "TimeAndSale"
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    trade_type: str | None = None
```

**Usage**: Input to rule engines (`engine.process(event)`).

#### `Alert`
```python
@dataclass(frozen=True, slots=True)
class Alert:
    """Represents a triggered alert from the rule engine."""
    symbol: str
    price: Decimal
    size: int
    timestamp_ms: int              # Epoch milliseconds
    rule_name: str
    severity: str = "high"         # info|low|medium|high|critical
    underlying_price: float | None = None  # Derived from Quote mid_price (bid+ask)/2
```

**Usage**: Output from rule engines, input to sinks.

#### `ConsolidatedSnapshot`
```python
@dataclass(slots=True)
class ConsolidatedSnapshot:
    """Latest merged state for a single symbol."""
    symbol: str
    underlying_symbol: str
    updated_at: datetime
    
    # Quote
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    
    # TimeAndSale
    last_trade_price: Decimal | None = None
    last_trade_size: int | None = None
    last_trade_time: int | None = None      # epoch ms
    last_trade_type: str | None = None
    
    # Derived
    mid_price: Decimal | None = None
    spread: Decimal | None = None
    spread_bps: float | None = None
    trade_vs_mid: Decimal | None = None
    
    # Lifecycle
    evict_at: int | None = None             # epoch ms
```

#### `ConsolidatedEvent`
```python
@dataclass(frozen=True, slots=True)
class ConsolidatedEvent:
    """Immutable representation of an incoming DXLink message."""
    event_id: int
    received_at: datetime
    source_type: Literal["QUOTE", "TIME_AND_SALE"]
    symbol: str
    
    # Quote fields
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    
    # TimeAndSale fields
    last_trade_price: Decimal | None = None
    last_trade_size: int | None = None
    last_trade_time: int | None = None
    last_trade_type: str | None = None
    
    # Raw timestamps
    event_time_ms: int | None = None
```

### Normalization Functions

#### `normalize_quote(q: DXQuote, event_id: int) -> ConsolidatedEvent`
Convert DXLink Quote to ConsolidatedEvent.

#### `normalize_timeandsale(tas: DXTimeAndSale, event_id: int) -> ConsolidatedEvent`
Convert DXLink TimeAndSale to ConsolidatedEvent.

#### `merge_into_snapshot(snap: ConsolidatedSnapshot, event: ConsolidatedEvent) -> ConsolidatedSnapshot`
Merge a ConsolidatedEvent into an existing snapshot (mutates and returns snap).

#### `snapshot_to_dict(snap: ConsolidatedSnapshot) -> dict[str, object]`
Convert snapshot to plain dict for Parquet serialization.

### Utility Functions

#### `_to_epoch_ms(ts: object) -> int | None`
Convert DXLink timestamp to epoch milliseconds.

```python
_to_epoch_ms(1722355200000)           # int/float → int
_to_epoch_ms("2024-07-30T16:00:00Z")  # ISO string → int
_to_epoch_ms(datetime.now(UTC))       # datetime → int
_to_epoch_ms(None)                    # None → None
```

#### `_parse_dt(ts: object) -> datetime | None`
Convert DXLink timestamp to datetime.

### Supporting Models

#### `StrikeInfo`
```python
@dataclass(frozen=True, slots=True)
class StrikeInfo:
    symbol: str
    strike: Decimal
    expiry: str
    option_type: str
```

#### `OptionRow`
```python
@dataclass(frozen=True, slots=True)
class OptionRow:
    streamer_symbol: str
    strike: Decimal
    expiry: str
    option_type: str
    last: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
```

#### `RollingStats`
```python
@dataclass(slots=True)
class RollingStats:
    symbol: str
    sizes: collections.deque[int] | None = None
    
    def __post_init__(self):
        if self.sizes is None:
            self.sizes = collections.deque(maxlen=50)
    
    def median(self) -> float
    def mad(self) -> float
```

---

## `dxlink_scanner.rules.cel_engine` — CEL Engine

### `CelAlertRule`
```python
class CelAlertRule(BaseModel):
    name: str
    expression: str
    severity: str = "high"  # info|low|medium|high|critical
```

### `CELRuleEngine`
```python
class CELRuleEngine:
    """CEL-based rule engine for per-symbol, underlying-scoped, and default alert rules."""
    
    def __init__(
        self,
        config: DetectionConfig,
        watchlist: WatchlistConfig,
        stats: RollingStatsManager | RollingStatsManagerV2,
        per_symbol_rules: dict[str, list[CelAlertRule]] | None = None,
        default_rules: list[CelAlertRule] | None = None,
        underlying_symbols: set[str] | None = None,
        underlying_symbol_map: dict[str, str] | None = None,
    ) -> None
```

#### `process(event: TimeAndSaleEvent) -> Alert | None`
Process a trade event through CEL rules.

**Evaluation Order**: Per-symbol rules → underlying-scoped rules → default rules. First match wins.

```python
alert = engine.process(event)
if alert:
    print(f"Rule: {alert.rule_name}, Severity: {alert.severity}")
```

#### `aprocess(event: TimeAndSaleEvent) -> Alert | None`
Async-compatible version (delegates to sync `process`).

#### `rule_count` (property)
Total number of compiled rules (per-symbol + underlying + defaults).

### Activation Context (Variables in CEL Expressions)

| Variable | Type | Description |
|----------|------|-------------|
| `trade` | `map<string, dyn>` | Core trade fields |
| `option` | `map<string, dyn>?` | Option metadata (if option symbol) |
| `underlying` | `map<string, dyn>?` | Underlying info (if underlying) |
| `stats` | `map<string, dyn>` | Rolling stats (median, mad, count, mean) |
| `config` | `map<string, dyn>` | Per-ticker thresholds |

See [cel_rules.md](cel_rules.md#activation-context) for full details.

---

## `dxlink_scanner.rules` — Package Exports

```python
from dxlink_scanner.rules import CELRuleEngine

engine = CELRuleEngine(...)
```

---

## `dxlink_scanner.sinks.stdout_sink` — Stdout Sink

### `_alert_to_dict(alert: Alert) -> dict[str, Any]`
Convert Alert to JSON-serializable dict.

**Output**:
```python
{
    "symbol": "...",
    "price": "2.50",
    "size": 150,
    "timestamp_ms": 1722355200000,
    "rule": "size_mult",
    "severity": "high",
    "underlying_price": 450.00,
}
```

### `_json_default(obj: Any) -> str`
JSON serializer for non-standard types. Handles `Decimal` → `str`. Raises `TypeError` for other types.

### `StdoutSink`

```python
class StdoutSink:
    """Output alerts to stdout as JSON lines."""
    
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
    
    async def send(self, alert: Alert) -> None:
        """Send an alert as a JSON line."""
```

**Usage**:
```python
sink = StdoutSink()
await sink.send(alert)
# Or with custom stream:
import io
stream = io.StringIO()
sink = StdoutSink(stream=stream)
await sink.send(alert)
print(stream.getvalue())
```

---

## `dxlink_scanner.sinks.webhook_sink` — Webhook Sink

### `WebhookSink`

```python
class WebhookSink:
    """HTTP webhook delivery with retry logic."""
    
    def __init__(
        self,
        url: str,
        timeout_seconds: float = 5.0,
        max_retries: int = 3,
    ) -> None
    
    async def send(self, alert: Alert) -> None:
        """Send alert via HTTP POST with exponential backoff retry."""
```

**Payload**: Same as `_alert_to_dict()` output.

**Retry Logic**: Exponential backoff (1s, 2s, 4s, ...) up to `max_retries`.

**Usage**:
```python
sink = WebhookSink(
    url="https://alerts.example.com/webhook",
    timeout_seconds=10.0,
    max_retries=5
)
await sink.send(alert)
```

---

## `dxlink_scanner.sinks` — Package Exports

```python
from dxlink_scanner.sinks import StdoutSink, WebhookSink
```

---

## `dxlink_scanner.stats` — Rolling Statistics Manager (V2)

### `RollingStatsV2`

```python
@dataclass(slots=True)
class RollingStatsV2:
    """Enhanced rolling statistics with exact sliding-window statistics."""

    symbol: str
    window_size: int = 50
    half_life_sec: float | None = None
    session_aware: bool = False

    # ... internal fields ...
```

**Features:**
- Exact sliding-window median, percentiles via sorted list (bisect)
- Weighted streaming mean/variance/std via Welford's algorithm with exponential time decay
- Exact MAD from window values
- Session-aware RTH/ETH separation
- Exponential time decay via `half_life_sec` (optional)

**Usage:**
```python
from dxlink_scanner.stats import RollingStatsV2

stats = RollingStatsV2(
    symbol="SPY",
    window_size=50,
    half_life_sec=3600,      # optional exponential decay
    session_aware=True       # RTH/ETH separation
)
stats.add(100, timestamp)
median = stats.median()
p95 = stats.percentile(95)
```

### `RollingStatsV2` Methods

| Method | Description |
|--------|-------------|
| `add(value: int, timestamp: datetime | None = None)` | Add a value to the rolling window |
| `clear()` | Clear all statistics |
| `reset()` | Alias for `clear()` |
| `median()` | Exact median from sorted list |
| `percentile(p: float)` | Exact percentile (0-100) with linear interpolation |
| `quantile(q: float)` | Exact quantile (0-1) |
| `mean()` | Weighted mean |
| `variance()` | Weighted sample variance |
| `std()` | Weighted standard deviation |
| `mad()` | Exact MAD from window values |
| `z_score(value: float)` | Standard z-score: `(value - mean) / std` |
| `modified_z_score(value: float)` | Modified z-score: `0.6745 * (value - median) / MAD` |

**Session-aware getters** (when `session_aware=True`):
| Method | Description |
|--------|-------------|
| `rth_median()` | RTH median |
| `rth_mean()` | RTH mean |
| `rth_std()` | RTH std |
| `rth_percentile(p)` | RTH percentile |
| `eth_median()` | ETH median |
| `eth_mean()` | ETH mean |
| `eth_std()` | ETH std |
| `eth_percentile(p)` | ETH percentile |

### `RollingStatsManagerV2`

```python
class RollingStatsManagerV2:
    """Manages rolling statistics across multiple symbols with V2 algorithm."""

    def __init__(self, config: DetectionConfig) -> None:
        # Reads stats_half_life_sec and stats_session_aware from config

    def add(self, symbol: str, size: int, timestamp: datetime | None = None) -> None:
        """Add a trade size to a symbol's rolling window."""

    def get(self, symbol: str) -> RollingStatsV2 | None:
        """Get rolling stats for a symbol, or None if not yet tracked."""

    def reset(self, symbol: str) -> None:
        """Reset statistics for a symbol."""

    def remove(self, symbol: str) -> bool:
        """Remove a symbol's statistics entirely."""

    def clear_all(self) -> None:
        """Clear all symbols' statistics."""

    def is_anomalous(self, symbol: str, size: int) -> tuple[bool, float, float]:
        """Check if a trade size is anomalous."""
        # Uses rule: size >= median * size_mult AND size >= abs_min_size
        # Returns (triggered, ratio, median_size)

    # Backward-compatible API
    def get_legacy(self, symbol: str):
        """Return legacy-compatible RollingStats interface."""
```

**Usage:**
```python
from dxlink_scanner.stats import RollingStatsManagerV2
from dxlink_scanner.config import DetectionConfig

config = DetectionConfig(stats_window=50, size_mult=5.0, abs_min_size=10)
mgr = RollingStatsManagerV2(config)

mgr.add("SPY", 100, timestamp)
stats = mgr.get("SPY")
print(stats.median(), stats.percentile(95))
```

### Configuration

`DetectionConfig` now supports V2 options:
```python
class DetectionConfig(BaseModel):
    size_mult: float = 5.0
    abs_min_size: int = 10
    stats_window: int = 50
    # V2 options
    stats_half_life_sec: float | None = None   # Exponential decay half-life (seconds)
    stats_session_aware: bool = False          # Enable RTH/ETH session separation
```

---

## `dxlink_scanner.snapshot_store` — Snapshot Store

### `SnapshotStore`

```python
class SnapshotStore:
    """In-memory consolidated snapshot store with parquet persistence."""
    
    def __init__(self, config: StreamConfig, persist: bool = True) -> None
    
    def set_evict_map(self, evicts: dict[str, int]) -> None
    def set_underlying_map(self, mapping: dict[str, str]) -> None
    
    def get(self, symbol: str) -> ConsolidatedSnapshot | None
    
    def ingest(self, event: ConsolidatedEvent) -> None
        """Ingest event: update snapshot + buffer for parquet."""
    
    async def start_flush_loop(self, output_dir: Path) -> None
    async def stop_flush_loop(self) -> None
    async def flush_remaining(self, output_dir: Path) -> None
    
    @property
    def snapshot_count(self) -> int
    @property
    def buffer_count(self) -> int
```

**Usage**:
```python
store = SnapshotStore(config.stream, persist=True)
store.set_evict_map(evict_map)
store.set_underlying_map(underlying_map)
await store.start_flush_loop(Path(config.outputs.data_dir))

# In consumer loop:
store.ingest(event)

# On shutdown:
await store.flush_remaining(output_dir)
await store.stop_flush_loop()
```

---

## `dxlink_scanner.schemas.v1` — Parquet Schema v1

### `schema_v1: pa.Schema`
```python
# Core ConsolidatedEvent fields + 3 raw timestamp columns
# All fields nullable for forward compatibility
```

**Fields** (14 total):
- Identity: `event_id`, `received_at`, `source_type`, `symbol`
- Quote: `bid_price`, `ask_price`
- TimeAndSale: `last_trade_price`, `last_trade_size`, `last_trade_time`, `last_trade_type`
- Raw timestamps: `event_time_ms`, `time_ms`, `time_nano_part_ms`
- Lifecycle: `evict_at`

**Usage**:
```python
from dxlink_scanner.schemas.v1 import schema_v1
import pyarrow as pa

table = pa.Table.from_pylist(rows, schema=schema_v1)
pq.write_table(table, "events.parquet")
```

---

## `dxlink_scanner.schemas.v2` — Parquet Schema v2

### `schema_v2: pa.Schema`
Extends v1 with derived Quote columns (all nullable):
- `mid_price` — (bid + ask) / 2, derived from Quote
- `spread` — ask - bid
- `spread_bps` — spread / mid * 10000
- `trade_vs_mid` — last_trade_price - mid_price

### `v2_new_fields: list[str]`
List of fields added in v2 (for migration).

### `v1_fields: list[str]`
List of all v1 fields (for migration backfill).

---

## `dxlink_scanner.cli` — CLI Entry Point

### `app` (Typer App)
```python
from dxlink_scanner.cli import app

# Run programmatically
app(["--config", "production.yaml"])
```

### CLI Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--config` | Path | Required | Config YAML file |
| `--verbose` | bool | `false` | Enable debug logging |
| `--debug-messages` | bool | `false` | Log every raw DXLink WebSocket message |

### Internal Functions

#### `_run_scanner(auth: TastyTradeAuth, config: ScannerConfig, debug_messages: bool, shutdown_event: asyncio.Event | None = None) -> None`
Main async entry point. Sets up auth, chains, streamer, store, rules, sinks, and runs the event loop.

#### `_consume_consolidated(...)`
Unified consumer loop (see architecture.md).

#### `_timeandsale_to_event(event: ConsolidatedEvent) -> TimeAndSaleEvent`
Convert ConsolidatedEvent (TAS) to TimeAndSaleEvent for rule engines.

---

## Complete Usage Example

```python
import asyncio
from decimal import Decimal
from datetime import datetime, UTC
from pathlib import Path

from dxlink_scanner.config import load_config, DetectionConfig, WatchlistConfig, TickerConfig
from dxlink_scanner.models import TimeAndSaleEvent, Alert
from dxlink_scanner.rules import CELRuleEngine

# 3. Create CEL engine with all rule tiers
engine = CELRuleEngine(
    config.detection, config.watchlist, stats_mgr,
    per_symbol_rules=per_symbol_rules,
    default_rules=config.watchlist.default_alert_rules,
    underlying_symbols=underlying_symbols_set,
    underlying_symbol_map=underlying_map,
)
engine.rule_count  # e.g. 32 (per-symbol + underlying + defaults)

# 4. Event processing loop
# ...

# 5. Evaluate rules
alert = engine.process(event)
if alert:
    for sink in sinks:
        await sink.send(alert)
```

---

## Type Aliases & Imports

```python
# Common imports
from dxlink_scanner.config import (
    ScannerConfig, TastyTradeConfig, TickerConfig, WatchlistConfig,
    DetectionConfig, CelAlertRule, WebhookConfig, OutputsConfig,
    LoggingConfig, StreamConfig, load_config
)
from dxlink_scanner.models import (
    TimeAndSaleEvent, Alert, ConsolidatedSnapshot, ConsolidatedEvent,
    StrikeInfo, OptionRow, RollingStats, merge_into_snapshot,
    _to_epoch_ms, _parse_dt
)
from dxlink_scanner.rules import CELRuleEngine
from dxlink_scanner.rules.cel_engine import CELRuleEngine as CELEngine, CelAlertRule
from dxlink_scanner.sinks import StdoutSink, WebhookSink
from dxlink_scanner.stats.rolling import RollingStatsManager, RollingStats
from dxlink_scanner.snapshot_store import SnapshotStore
from dxlink_scanner.schemas.v1 import schema_v1
from dxlink_scanner.schemas.v2 import schema_v2, v2_new_fields, v1_fields
```

---

*See also: [architecture.md](architecture.md), [configuration.md](configuration.md), [cel_rules.md](cel_rules.md), [deployment.md](deployment.md)*