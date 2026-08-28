# SPEC: Strike Filtering & Subscription Batching for DXLink Limits

**Status**: 🟡 Design | **Author**: Hermes Agent | **Date**: 2026-08-28

---

## 1. Problem Statement

The DXLink Scanner currently passes all option streamer symbols in a single `streamer.subscribe()` call. The full 0DTE option chain for SPY, SPX, and /ES contains **~850–1,300 strikes**. Each symbol string is ~25–30 characters (e.g., `.SPY260731C00500000:EQ`), so the combined subscription payload is:

- **TimeAndSale**: ~1,300 symbols × ~30 chars = ~39KB raw, plus JSON framing, topic metadata, and message overhead → **exceeds 64KB**
- **TheoPrice**: Same issue

**DXLink imposes a hard 64KB limit on individual WebSocket messages** (both control and data). When a subscription message exceeds 64KB, the server rejects it, the connection drops, or events are silently lost.

From the [dxFeed FAQ](https://kb.dxfeed.com/en/faq.html):

> *"To subscribe to a large number of events and symbols, split your subscription requests into smaller chunks: each message shouldn't exceed 64 KB in size. For example, to subscribe to 1,000 topics (event + symbol), send five separate subscription messages with 200 topics in each."*

**Two complementary fixes are needed:**
1. **Subscription batching** (primary) — split subscribe calls to stay under 64KB per message
2. **Strike filtering** (secondary) — reduce subscribed symbol count to lower memory, event rate, and noise

---

## 2. Constraints

| Constraint | Detail |
|---|---|
| **DXLink message size limit** | 64 KB per WebSocket message (control and data) |
| **DXLink symbol count limit** | No hard limit on total subscriptions; B2C apps typically 5,000–15,000 |
| **DXLink throughput** | ~500,000 events/sec per connection |
| **0DTE chain depth** | SPY: ~300–400 strikes; SPX: ~300–500 strikes; /ES: ~250–400 strikes |
| **Strike spacing** | SPY: $1 near ATM; SPX: $5 near ATM; /ES: $5 near ATM |
| **Actionable range** | Strikes within ±3–5% of underlying price contain >99% of volume/OI |
| **Delta availability** | TheoPrice stream provides live delta; BS estimate available as fallback |
| **Intraday moves** | Underlying can move 1–3% in session; filter must adapt or be wide enough |
| **tastytrade SDK** | `DXLinkStreamer.subscribe()` sends one subscription message per call |

---

## 3. Current State Analysis

### 3.1 Current Symbol Counts (Approximate)

| Underlying | 0DTE Strikes (Calls + Puts) | Streamer Symbol Length |
|---|---|---|
| **SPY** | ~300–400 | `.SPY260731C00500000:EQ` (~25 chars) |
| **SPX** | ~300–500 | `SPX260731C0050000` (~18 chars) |
| **/ES** | ~250–400 | `.ES260731C00500000` (~22 chars) |
| **Total** | **~850–1,300** | — |

### 3.2 Current Subscription Payload Size

Current code in `cli.py`:
```python
await streamer.subscribe(Quote, underlying_symbols)           # ~6 symbols — OK
await streamer.subscribe(DXTimeAndSale, underlying_symbols + all_symbols)  # ~1,306 symbols — OVER 64KB
await streamer.subscribe(DXTheoPrice, all_symbols)            # ~1,300 symbols — OVER 64KB
```

**Estimated payload per message (JSON):**
- Each topic in a subscription message includes event type + symbol string
- Average symbol string: ~25 chars
- JSON overhead per topic: ~50 chars (`{"eventType":"TIME_AND_SALE","symbol":"..."}`)
- **Per topic: ~75 bytes**
- **1,300 topics × 75 bytes = ~97.5 KB → exceeds 64KB**

### 3.3 Required Chunk Size

To stay under 64KB with margin:
- **64KB / 75 bytes per topic ≈ 850 topics maximum**
- **Safe target: 100–150 topics per subscription message**
- **For 1,300 symbols: ~9–13 chunks of 100–150 symbols each**

---

## 4. Solution 1: Subscription Batching (PRIMARY FIX)

### 4.1 Problem

A single `streamer.subscribe()` call with 1,300 symbols generates a subscription message exceeding 64KB, which the DXLink server rejects.

### 4.2 Design: Batched Subscription Helper

Add a utility that splits large symbol lists into chunks and issues separate subscribe calls for each chunk:

```python
# src/dxlink_scanner/stream_utils.py

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tastytrade.streamer import DXLinkStreamer

logger = logging.getLogger(__name__)

# DXLink message size limit: 64KB
# Each topic in subscription message ≈ 75 bytes (JSON overhead + symbol string)
# Safe margin: target 50KB max → ~650 topics, but use 150 for safety + latency
DEFAULT_SUBSCRIPTION_CHUNK_SIZE = 150

# Delay between chunks to avoid overwhelming the server
SUBSCRIPTION_CHUNK_DELAY_SEC = 0.05


async def subscribe_batched(
    streamer: DXLinkStreamer,
    event_type: type,
    symbols: list[str],
    chunk_size: int = DEFAULT_SUBSCRIPTION_CHUNK_SIZE,
    delay_sec: float = SUBSCRIPTION_CHUNK_DELAY_SEC,
) -> int:
    """Subscribe to symbols in chunks to stay within DXLink's 64KB message limit.
    
    DXLink rejects individual WebSocket messages exceeding 64KB. This function
    splits large symbol lists into chunks and issues separate subscribe() calls
    for each chunk, with a small delay between chunks.
    
    Args:
        streamer: Connected DXLinkStreamer instance.
        event_type: Event type to subscribe to (Quote, TimeAndSale, TheoPrice).
        symbols: List of streamer symbols to subscribe to.
        chunk_size: Maximum symbols per subscription message (default 150).
        delay_sec: Delay between chunked subscribe calls (default 0.05s).
    
    Returns:
        Total number of chunks sent.
    
    Note:
        Each subscribe() call creates a separate topic subscription on the
        server. The server deduplicates topics automatically, so subscribing
        to the same symbol in multiple chunks is idempotent.
    """
    if not symbols:
        return 0
    
    chunks = [
        symbols[i : i + chunk_size]
        for i in range(0, len(symbols), chunk_size)
    ]
    
    total_chunks = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        await streamer.subscribe(event_type, chunk)
        logger.debug(
            "Subscribed chunk %d/%d: %d %s symbols",
            idx,
            total_chunks,
            len(chunk),
            event_type.__name__,
        )
        if idx < total_chunks:
            await asyncio.sleep(delay_sec)
    
    logger.info(
        "Subscribed %d %s symbols in %d chunks (chunk_size=%d)",
        len(symbols),
        event_type.__name__,
        total_chunks,
        chunk_size,
    )
    return total_chunks


async def unsubscribe_batched(
    streamer: DXLinkStreamer,
    event_type: type,
    symbols: list[str],
    chunk_size: int = DEFAULT_SUBSCRIPTION_CHUNK_SIZE,
    delay_sec: float = SUBSCRIPTION_CHUNK_DELAY_SEC,
) -> int:
    """Unsubscribe from symbols in chunks.
    
    Same chunking strategy as subscribe_batched() for unsubscribe operations.
    """
    if not symbols:
        return 0
    
    chunks = [
        symbols[i : i + chunk_size]
        for i in range(0, len(symbols), chunk_size)
    ]
    
    total_chunks = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        await streamer.unsubscribe(event_type, chunk)
        if idx < total_chunks:
            await asyncio.sleep(delay_sec)
    
    logger.info(
        "Unsubscribed %d %s symbols in %d chunks",
        len(symbols),
        event_type.__name__,
        total_chunks,
    )
    return total_chunks
```

### 4.3 Integration into `cli.py`

Replace the existing subscribe calls in `_run_scanner()`:

```python
# BEFORE (broken for large symbol counts):
async with DXLinkStreamer(session) as streamer:
    await streamer.subscribe(Quote, underlying_symbols)
    await streamer.subscribe(DXTimeAndSale, underlying_symbols + all_symbols)
    if all_symbols:
        await streamer.subscribe(DXTheoPrice, all_symbols)

# AFTER (chunked to stay under 64KB):
from dxlink_scanner.stream_utils import subscribe_batched

async with DXLinkStreamer(session) as streamer:
    tas_symbols = underlying_symbols + all_symbols
    
    quote_chunks = await subscribe_batched(streamer, Quote, underlying_symbols)
    tas_chunks = await subscribe_batched(streamer, DXTimeAndSale, tas_symbols)
    theo_chunks = 0
    if all_symbols:
        theo_chunks = await subscribe_batched(streamer, DXTheoPrice, all_symbols)
    
    logger.info(
        "Subscribed: Quote(%d chunks), TimeAndSale(%d chunks), TheoPrice(%d chunks)",
        quote_chunks,
        tas_chunks,
        theo_chunks,
    )
```

### 4.4 Integration into `dynamic_strikes.py`

The rescan loop also subscribes to new symbols:

```python
# BEFORE:
if delta.added:
    await streamer.subscribe(TimeAndSale, delta.added)

# AFTER:
from dxlink_scanner.stream_utils import subscribe_batched, unsubscribe_batched

if delta.added:
    await subscribe_batched(streamer, DXTimeAndSale, delta.added)
if delta.removed:
    await unsubscribe_batched(streamer, DXTimeAndSale, delta.removed)
```

### 4.5 Config Options

```python
class StreamConfig(BaseModel):
    # Existing fields...
    backpressure_queue_size: int = Field(default=500, gt=0, le=100000)
    flush_interval_sec: float = Field(default=5.0, gt=0)
    flush_batch_size: int = Field(default=10000, gt=0, le=1000000)
    tick_size: float = Field(default=0.01, gt=0)
    
    # New: subscription batching
    subscription_chunk_size: int = Field(
        default=150,
        ge=10,
        le=500,
        description="Max symbols per subscription message (DXLink 64KB limit). "
        "Each topic ~75 bytes, 150 topics ≈ 11KB with margin.",
    )
    subscription_chunk_delay_sec: float = Field(
        default=0.05,
        ge=0,
        le=5.0,
        description="Delay between chunked subscribe calls to avoid server overload.",
    )
```

### 4.6 Symbol Size Estimation Utility

Add a utility to estimate subscription message size and warn if approaching the limit:

```python
def estimate_subscription_message_size(
    event_type: type,
    symbols: list[str],
) -> int:
    """Estimate the byte size of a subscription message.
    
    Args:
        event_type: The DXLink event type being subscribed to.
        symbols: List of streamer symbols.
    
    Returns:
        Estimated message size in bytes.
    """
    # JSON envelope: {"type":"BOOK","add":[...]}
    envelope_overhead = 200
    # Per topic: {"eventType":"TIME_AND_SALE","symbol":"..."}
    per_topic_overhead = 50
    symbol_bytes = sum(len(s.encode("utf-8")) for s in symbols)
    return envelope_overhead + per_topic_overhead * len(symbols) + symbol_bytes


def will_exceed_message_limit(
    event_type: type,
    symbols: list[str],
    limit_bytes: int = 64 * 1024,
) -> bool:
    """Check if a subscription message would exceed DXLink's 64KB limit."""
    return estimate_subscription_message_size(event_type, symbols) > limit_bytes
```

---

## 5. Solution 2: Strike Filtering (SECONDARY OPTIMIZATION)

While subscription batching solves the 64KB problem, strike filtering remains valuable to:
- **Reduce memory** (fewer `ConsolidatedSnapshot` objects)
- **Reduce event rate** (fewer symbols generating events = less queue pressure)
- **Reduce noise** (far OTM symbols with no activity still generate Quote heartbeats)
- **Stay well within DXLink limits** (headroom for additional underlyings like QQQ, IWM)

### 5.1 Filtering Options

#### Option A: Percentage Band Around ATM (RECOMMENDED)

Select strikes within ±X% of the current underlying price.

```yaml
strike_filter:
  mode: "percent"
  percent: 4.0        # ±4% of underlying price
```

**Pros:**
- Intuitive — directly maps to "how far from the money"
- No delta dependency
- Simple to reason about
- Captures >99% of actionable volume for 0DTE

**Cons:**
- Doesn't adapt to IV changes
- Doesn't account for intraday moves (need refresh or wide band)

#### Option B: Delta-Based Filter

Select strikes where `|delta| >= threshold`.

```yaml
strike_filter:
  mode: "delta"
  min_abs_delta: 0.05
```

**Pros:**
- Economically meaningful
- Automatically adapts to IV

**Cons:**
- Requires TheoPrice subscription first (chicken-and-egg with batching)
- Delta changes with price and time

#### Option C: Hybrid (Percentage + Delta Floor)

```yaml
strike_filter:
  mode: "hybrid"
  percent: 5.0           # Initial wide band
  min_abs_delta: 0.02    # Exclude dead strikes within band
  refresh_interval_min: 60
```

**Pros:**
- Most robust — band captures range, delta floor removes dead strikes
- Adapts via refresh

**Cons:**
- More complex (two parameters, delta dependency)

#### Option D: Fixed Count

```yaml
strike_filter:
  mode: "count"
  count: 50
```

**Pros:**
- Deterministic symbol count
- Trivial to implement

**Cons:**
- Arbitrary — doesn't adapt to anything

### 5.2 Recommendation: Option A (Percent Band)

**Use a simple percent band as the default strike filter.**

Rationale:
- **Simple** — one parameter, no delta dependency
- **Effective** — captures the economically active range
- **Predictable** — easy to estimate symbol count
- **Works with batching** — no chicken-and-egg problem

Hybrid mode is available for users who want stricter filtering.

### 5.3 Config Schema

```python
class StrikeFilterConfig(BaseModel):
    """Strike filtering configuration for option chain subscription.
    
    Reduces the number of subscribed symbols by excluding far OTM strikes
    that have no trade activity. Used in addition to subscription batching
    to keep total symbol count manageable.
    """
    mode: str = Field(
        default="percent",
        pattern=r"^(none|count|percent|delta|hybrid)$",
    )
    # Count mode
    count: int = Field(default=50, ge=1, le=500)
    # Percent mode / Hybrid band
    percent: float = Field(default=4.0, gt=0, le=50)
    # Delta mode / Hybrid floor
    min_abs_delta: float = Field(default=0.02, ge=0, le=1)
    # Refresh interval for re-evaluating ATM center
    refresh_interval_min: int = Field(default=60, ge=5, le=240)


class TickerConfig(BaseModel):
    symbol: str
    option_type: str = Field(default="equity", pattern=r"^(equity|futures)$")
    expiration_filter: str = Field(default="0DTE", pattern=r"^(0DTE|all)$")
    strike_filter: StrikeFilterConfig = Field(default_factory=StrikeFilterConfig)
    alert_rules: list[CelAlertRule] = Field(default_factory=list)
    underlying_alert_rules: list[CelAlertRule] = Field(default_factory=list)
```

### 5.4 YAML Configuration

```yaml
watchlist:
  tickers:
    - symbol: "SPY"
      option_type: "equity"
      expiration_filter: "0DTE"
      strike_filter:
        mode: "percent"
        percent: 4.0
        refresh_interval_min: 60

    - symbol: "SPX"
      option_type: "equity"
      expiration_filter: "0DTE"
      strike_filter:
        mode: "percent"
        percent: 5.0

    - symbol: "/ES"
      option_type: "futures"
      expiration_filter: "0DTE"
      strike_filter:
        mode: "percent"
        percent: 5.0

stream:
  subscription_chunk_size: 150
  subscription_chunk_delay_sec: 0.05
```

### 5.5 Filtering Logic

New module: `src/dxlink_scanner/strike_filter.py`

```python
"""Strike filtering to reduce subscription count and memory footprint."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from dxlink_scanner.config import StrikeFilterConfig
from dxlink_scanner.models import StrikeInfo

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FilterResult:
    """Result of applying a strike filter."""
    included: list[StrikeInfo]
    excluded: list[StrikeInfo]
    underlying_price: Decimal | None
    
    @property
    def reduction_pct(self) -> float:
        total = len(self.included) + len(self.excluded)
        if total == 0:
            return 0.0
        return len(self.excluded) / total * 100


def filter_strikes(
    strikes: list[StrikeInfo],
    config: StrikeFilterConfig,
    underlying_price: Decimal | None,
    delta_map: dict[str, Decimal] | None = None,
) -> FilterResult:
    """Filter strikes based on the configured mode.
    
    Args:
        strikes: All strikes from the option chain (single option type).
        config: Strike filter configuration.
        underlying_price: Current underlying price.
        delta_map: Optional map of streamer_symbol → delta.
    
    Returns:
        FilterResult with included/excluded strikes.
    """
    if config.mode == "none" or not strikes:
        return FilterResult(included=strikes, excluded=[], underlying_price=underlying_price)
    
    if config.mode == "count":
        return _filter_by_count(strikes, config.count, underlying_price)
    elif config.mode == "percent":
        return _filter_by_percent(strikes, config.percent, underlying_price)
    elif config.mode == "delta":
        return _filter_by_delta(strikes, config.min_abs_delta, delta_map)
    elif config.mode == "hybrid":
        return _filter_hybrid(strikes, config, underlying_price, delta_map)
    else:
        raise ValueError(f"Unknown filter mode: {config.mode}")


def _filter_by_count(
    strikes: list[StrikeInfo],
    count: int,
    underlying_price: Decimal | None,
) -> FilterResult:
    """Select N strikes closest to ATM."""
    if underlying_price is None or not strikes:
        # Fallback: just take first N
        return FilterResult(
            included=strikes[:count],
            excluded=strikes[count:],
            underlying_price=underlying_price,
        )
    
    sorted_strikes = sorted(strikes, key=lambda s: abs(s.strike - underlying_price))
    return FilterResult(
        included=sorted_strikes[:count],
        excluded=sorted_strikes[count:],
        underlying_price=underlying_price,
    )


def _filter_by_percent(
    strikes: list[StrikeInfo],
    percent: float,
    underlying_price: Decimal | None,
) -> FilterResult:
    """Select strikes within ±X% of underlying price."""
    if underlying_price is None:
        logger.warning("No underlying price for percent filter; including all strikes")
        return FilterResult(included=strikes, excluded=[], underlying_price=None)
    
    band = underlying_price * Decimal(str(percent / 100))
    included = [s for s in strikes if abs(s.strike - underlying_price) <= band]
    excluded = [s for s in strikes if abs(s.strike - underlying_price) > band]
    
    return FilterResult(
        included=included,
        excluded=excluded,
        underlying_price=underlying_price,
    )


def _filter_by_delta(
    strikes: list[StrikeInfo],
    min_abs_delta: float,
    delta_map: dict[str, Decimal] | None,
) -> FilterResult:
    """Select strikes where |delta| >= threshold."""
    if delta_map is None:
        logger.warning("No delta_map for delta filter; including all strikes")
        return FilterResult(included=strikes, excluded=[], underlying_price=None)
    
    min_d = Decimal(str(min_abs_delta))
    included = []
    excluded = []
    for s in strikes:
        d = delta_map.get(s.symbol)
        if d is not None and abs(d) >= min_d:
            included.append(s)
        else:
            excluded.append(s)
    return FilterResult(included=included, excluded=excluded, underlying_price=None)


def _filter_hybrid(
    strikes: list[StrikeInfo],
    config: StrikeFilterConfig,
    underlying_price: Decimal | None,
    delta_map: dict[str, Decimal] | None,
) -> FilterResult:
    """Hybrid: percent band first, then delta floor."""
    band_result = _filter_by_percent(strikes, config.percent, underlying_price)
    
    if delta_map is None:
        return band_result
    
    min_d = Decimal(str(config.min_abs_delta))
    included = []
    excluded = list(band_result.excluded)
    
    for s in band_result.included:
        d = delta_map.get(s.symbol)
        if d is not None and abs(d) >= min_d:
            included.append(s)
        else:
            excluded.append(s)
    
    return FilterResult(
        included=included,
        excluded=excluded,
        underlying_price=underlying_price,
    )


def apply_filter_to_chain(
    strikes_by_type: dict[str, list[StrikeInfo]],
    config: StrikeFilterConfig,
    underlying_price: Decimal | None,
    delta_map: dict[str, Decimal] | None = None,
) -> tuple[list[StrikeInfo], FilterResult]:
    """Apply filter to a mixed chain (calls + puts).
    
    Args:
        strikes_by_type: Dict mapping option_type ("call"/"put") to StrikeInfo lists.
        config: Filter configuration.
        underlying_price: Current underlying price.
        delta_map: Optional delta map.
    
    Returns:
        Tuple of (all_included_strikes, last_filter_result).
    """
    all_included: list[StrikeInfo] = []
    all_excluded: list[StrikeInfo] = []
    last_result = None
    
    for option_type, strikes in strikes_by_type.items():
        result = filter_strikes(strikes, config, underlying_price, delta_map)
        all_included.extend(result.included)
        all_excluded.extend(result.excluded)
        last_result = result
    
    return all_included, last_result
```

### 5.6 Integration into `bootstrap.py`

```python
def parse_chain(
    chains: dict[date, list[Option | FutureOption]],
    expiration_filter: str = "0DTE",
    is_future: bool = False,
    strike_filter: StrikeFilterConfig | None = None,
    underlying_price: Decimal | None = None,
) -> tuple[UnderlyingInfo, list[StrikeInfo]]:
    """Parse SDK option chain result into typed structures.
    
    When strike_filter is provided, applies filtering to reduce the
    number of returned strikes.
    """
    # ... existing logic to extract all strikes ...
    
    if strike_filter is not None and strike_filter.mode != "none" and underlying_price is not None:
        from dxlink_scanner.strike_filter import filter_strikes
        
        all_strikes = strikes  # Already extracted above
        result = filter_strikes(all_strikes, strike_filter, underlying_price)
        
        logger.info(
            "Strike filter [%s] mode=%s: %d strikes → %d included, %d excluded (%.1f%% reduction)",
            ticker_symbol := "unknown",
            strike_filter.mode,
            len(all_strikes),
            len(result.included),
            len(result.excluded),
            result.reduction_pct,
        )
        strikes = result.included
    
    return underlying_info, strikes
```

### 5.7 Integration into `cli.py`

```python
from dxlink_scanner.strike_filter import filter_strikes
from dxlink_scanner.stream_utils import subscribe_batched, estimate_subscription_message_size

# In _run_scanner():
for ticker in config.watchlist.tickers:
    chains = await loader.get_nested_chain(ticker.symbol, ticker.option_type)
    underlying_price = await loader.get_underlying_price(price_symbol, ticker.option_type)
    
    underlying_info, strikes = loader.parse_chain(
        chains,
        ticker.expiration_filter,
        is_future=ticker.option_type == "futures",
        strike_filter=ticker.strike_filter,
        underlying_price=underlying_price,
    )
    
    # Log estimated subscription message size
    est_size = estimate_subscription_message_size(DXTimeAndSale, [s.symbol for s in strikes])
    logger.info(
        "Estimated TimeAndSale subscription size: %.1f KB for %d symbols",
        est_size / 1024,
        len(strikes),
    )
    
    symbols = [s.symbol for s in strikes]
    all_symbols.extend(symbols)
    # ... rest of setup ...
```

### 5.8 Integration into `dynamic_strikes.py`

Update `_collect_symbols()` to apply the filter on each rescan:

```python
async def _collect_symbols(self, ticker):
    chains = await self._loader.get_nested_chain(ticker.symbol, ticker.option_type)
    underlying_price = await self._loader.get_underlying_price(
        ticker.symbol, ticker.option_type
    )
    underlying_info, strikes = self._loader.parse_chain(
        chains,
        ticker.expiration_filter,
        is_future=ticker.option_type == "futures",
        strike_filter=ticker.strike_filter,
        underlying_price=underlying_price,
    )
    symbols = [s.symbol for s in strikes]
    return ticker.symbol, symbols, underlying_info.symbol
```

---

## 6. Combined Impact Analysis

### 6.1 Before (Current State)

| Metric | Value |
|---|---|
| SPY 0DTE strikes | ~350 |
| SPX 0DTE strikes | ~400 |
| /ES 0DTE strikes | ~350 |
| **Total symbols** | **~1,100** |
| TimeAndSale subscription size | ~82 KB (OVER 64KB) |
| TheoPrice subscription size | ~82 KB (OVER 64KB) |
| SnapshotStore entries | ~1,100 |
| Memory estimate | ~2.2 MB |

### 6.2 After Subscription Batching Only

| Metric | Value |
|---|---|
| Total symbols | ~1,100 (unchanged) |
| TimeAndSale chunks | ~8 chunks of 150 |
| TheoPrice chunks | ~8 chunks of 150 |
| Per-chunk size | ~11 KB (under 64KB ✅) |
| SnapshotStore entries | ~1,100 |
| Memory estimate | ~2.2 MB |

**Problem solved:** 64KB limit respected. No more connection drops.

### 6.3 After Strike Filtering + Batching

| Metric | Value |
|---|---|
| SPY strikes (4% band) | ~80–120 |
| SPX strikes (5% band) | ~100–150 |
| /ES strikes (5% band) | ~100–150 |
| **Total symbols** | **~280–420** |
| TimeAndSale chunks | ~2–3 chunks |
| TheoPrice chunks | ~2–3 chunks |
| SnapshotStore entries | ~280–420 |
| Memory estimate | ~0.6–0.8 MB |
| **Reduction** | **~60–75%** |

**Problem solved:** 64KB limit respected. Lower memory. Less noise. Room to grow.

### 6.4 Adding More Underlyings (QQQ, IWM, etc.)

With filtering + batching, adding more underlyings is straightforward:

| Configuration | Symbols | Chunks | Memory |
|---|---|---|---|
| SPY + SPX + /ES (current) | 1,100 | 8 | 2.2 MB |
| SPY + SPX + /ES (filtered) | 350 | 3 | 0.7 MB |
| SPY + SPX + /ES + QQQ + IWM (filtered) | ~600 | 4 | 1.2 MB |
| All above + AAPL + TSLA (filtered) | ~900 | 6 | 1.8 MB |

---

## 7. Sprint Breakdown

### Sprint 1: Subscription Batching (2 days)

**Goal:** Fix the immediate 64KB subscription message problem.

| Task | Description | Deliverable |
|---|---|---|
| 1.1 | Create `src/dxlink_scanner/stream_utils.py` with `subscribe_batched()` / `unsubscribe_batched()` | Batch utility |
| 1.2 | Add `subscription_chunk_size` / `subscription_chunk_delay_sec` to `StreamConfig` | Config schema |
| 1.3 | Update `cli.py:_run_scanner()` to use batched subscriptions | Fixed startup |
| 1.4 | Update `dynamic_strikes.py:_rescan_loop()` to use batched subscribe/unsubscribe | Fixed rescan |
| 1.5 | Add `estimate_subscription_message_size()` utility | Size estimation |
| 1.6 | Unit tests for batching logic and size estimation | `tests/test_stream_utils.py` |
| 1.7 | Integration test: verify chunk count for N symbols | `tests/test_batching_integration.py` |

**Acceptance:** Scanner starts successfully with 1,100+ symbols. No 64KB errors. Log shows chunk count.

---

### Sprint 2: Strike Filter Logic (2 days)

**Goal:** Implement `strike_filter.py` with all filter modes.

| Task | Description | Deliverable |
|---|---|---|
| 2.1 | Create `StrikeFilterConfig` Pydantic model in `config/__init__.py` | Config schema |
| 2.2 | Implement `strike_filter.py` with `filter_strikes()` and all modes | Filter module |
| 2.3 | Unit tests for each filter mode | `tests/test_strike_filter.py` |
| 2.4 | Edge cases: empty strikes, no underlying price, missing delta_map | Test coverage |

**Acceptance:** All filter modes produce correct included/excluded lists. 100% branch coverage.

---

### Sprint 3: Bootstrap & CLI Integration (2 days)

**Goal:** Wire strike filter into `parse_chain()` and `cli.py`.

| Task | Description | Deliverable |
|---|---|---|
| 3.1 | Add `strike_filter` param to `parse_chain()` | Updated bootstrap.py |
| 3.2 | Update `cli.py:_run_scanner()` to pass filter and log results | Updated cli.py |
| 3.3 | Update `dynamic_strikes.py` to pass filter on rescan | Updated dynamic_strikes.py |
| 3.4 | Log estimated subscription message size and chunk count | Logging |
| 3.5 | Integration test: full chain → filter → batch → subscribe | `tests/test_filter_integration.py` |

**Acceptance:** Scanner starts with filtered + batched strike count. Logs show reduction % and chunk count.

---

### Sprint 4: Documentation & Polish (1 day)

**Goal:** Document the feature and update all references.

| Task | Description | Deliverable |
|---|---|---|
| 4.1 | Update `docs/configuration.md` with `strike_filter` and `stream` batching | Config docs |
| 4.2 | Update `docs/api_reference.md` with new models and utilities | API docs |
| 4.3 | Update `docs/index.md` example config | Index docs |
| 4.4 | Update `production.yaml` with recommended settings | Config template |
| 4.5 | Update `ROADMAP.md` | Roadmap |

**Acceptance:** All docs reflect the new features. Example config is copy-paste ready.

---

## 8. Backward Compatibility

### 8.1 Subscription Batching

- Default `subscription_chunk_size: 150` — safe for any symbol count
- Small symbol counts (< 150) → single chunk, identical behavior to before
- Existing configs work unchanged

### 8.2 Strike Filtering

- Default `mode: "percent"` with `percent: 4.0`
- `mode: "none"` preserves current behavior (subscribe to all strikes)
- Existing configs without `strike_filter` get sensible defaults via Pydantic
- No breaking changes to `TickerConfig` — new field has a default

---

## 9. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| tastytrade SDK batches internally | Our batching is redundant | Test with large symbol counts; if SDK already chunks, our batching is a no-op |
| Server rate-limits chunked subscriptions | Subscription delays | Add configurable delay between chunks (default 50ms) |
| Underlying moves beyond filter band | Miss trades on newly-ATM strikes | Refresh filter on rescan; set band wide enough (±4–5%) |
| Chunk size too large for short symbol lists | Unnecessary batching | `subscribe_batched()` handles small lists as single chunk |
| Chunk size still too large with very long symbol strings | Still exceed 64KB | Make `subscription_chunk_size` configurable; provide size estimation utility |
| TheoPrice unavailable at startup for delta filter | Delta floor can't be applied | Fall back to percent-only filtering; log warning |

---

## 10. Success Metrics

| Metric | Target | Measurement |
|---|---|---|
| Subscription message size | ≤ 50 KB (under 64KB with margin) | `estimate_subscription_message_size()` |
| Per-chunk symbols | ≤ 150 | Startup log |
| Filter reduction rate | ≥ 50% | `FilterResult.reduction_pct` |
| SnapshotStore entries | ≤ 500 (filtered) | Memory profiling |
| Scanner startup success | 100% with 1,000+ symbols | No subscription errors |
| DXLink connection stability | No drops after batching | Connection uptime |

---

## 11. Future Enhancements

- **Adaptive percent band**: Adjust band width based on realized vol
- **Per-regime filtering**: Wider band in crash regime (tail risk monitoring)
- **Dynamic chunk sizing**: Auto-tune `subscription_chunk_size` based on symbol string lengths
- **Symbol count dashboard**: Real-time monitoring of active subscriptions per chunk
- **Multi-connection splitting**: If single connection insufficient, split across multiple DXLink connections
- **Subscription health monitoring**: Track per-chunk subscription status and retry on failure
