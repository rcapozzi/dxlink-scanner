# SPEC: Strike Filtering for DXLink Subscription Limits

**Status**: 🟡 Design | **Author**: Hermes Agent | **Date**: 2026-08-28

---

## 1. Problem Statement

The DXLink Scanner currently subscribes to **all 0DTE option strikes** for each configured underlying (SPY, SPX, /ES). The full option chain for these underlyings is long — many strikes exist far out of the money (OTM) solely to define risk for deep ITM counterparties. These far OTM strikes have:

- **Zero or near-zero delta** (≈0.0001–0.01)
- **No trade activity** (no TimeAndSale events)
- **No open interest** or negligible OI
- **No bid/ask** or extremely wide spreads

Subscribing to them wastes DXLink subscription slots, consumes WebSocket bandwidth, and inflates the SnapshotStore memory footprint — all for symbols that will never produce actionable data.

**DXLink imposes a hard limit on the number of symbols that can be subscribed per connection** (practical limit ≈ 500–1000 symbols). With SPY + SPX + /ES 0DTE chains combined, the total strike count routinely exceeds this limit, causing subscription failures or degraded streaming quality.

---

## 2. Constraints

| Constraint | Detail |
|---|---|
| **DXLink symbol limit** | ~500–1000 symbols per WebSocket connection (Tastytrade-enforced) |
| **0DTE chain depth** | SPY: ~200–400 strikes; SPX: ~200–500 strikes; /ES: ~200–400 strikes |
| **Strike spacing** | SPY: $1 near ATM, $5 wide; SPX: $5 near ATM, $25 wide; /ES: $5 near ATM, $25 wide |
| **Actionable range** | Strikes within ±3–5% of underlying price contain >99% of volume/OI |
| **Delta availability** | TheoPrice stream provides live delta; SDK chain provides no delta directly |
| **Intraday moves** | Underlying can move 1–3% in a session; filter must adapt or be wide enough |
| **Risk definition** | Far OTM strikes exist to define risk for far ITM trades — we don't need to *watch* them, we just need to know they *exist* |

---

## 3. Current 0DTE Strike Counts (Approximate)

| Underlying | Price Range | Strike Spacing | 0DTE Strikes (Calls + Puts) | Actionable Range (±3%) |
|---|---|---|---|---|
| **SPY** | ~$550 | $1 near ATM, $5 wings | ~300–400 | ±$16.50 → ~33 strikes/side |
| **SPX** | ~$5,500 | $5 near ATM, $25 wings | ~300–500 | ±$165 → ~33 strikes/side |
| **/ES** | ~$5,500 | $5 near ATM, $25 wings | ~250–400 | ±$165 → ~33 strikes/side |
| **Total** | — | — | **~850–1,300** | **~200 strikes total** |

**Key insight**: Only ~15–20% of strikes fall within ±3% of ATM. The remaining 80–85% are far OTM with negligible activity.

---

## 4. Filtering Options

### Option A: Fixed Count Around ATM (Current Approach, Refined)

Select N strikes closest to the current underlying price.

```yaml
watchlist:
  tickers:
    - symbol: "SPY"
      strike_filter:
        mode: "count"
        count: 50          # 25 calls + 25 puts around ATM
```

**Pros:**
- Simple, deterministic symbol count
- Easy to reason about DXLink budget
- No dependency on delta data

**Cons:**
- Fixed count doesn't adapt to IV changes (high IV → need more strikes)
- Doesn't account for intraday price moves (ATM shifts)
- Arbitrary — why 50? why not 40 or 60?

---

### Option B: Delta-Based Filter

Select strikes where `|delta| >= threshold` (e.g., 0.05 ≤ |delta| ≤ 0.95).

```yaml
watchlist:
  tickers:
    - symbol: "SPY"
      strike_filter:
        mode: "delta"
        min_delta: 0.05
        max_delta: 0.95
```

**Pros:**
- Economically meaningful — selects strikes with non-trivial probability of expiring ITM
- Automatically adapts to IV changes (delta incorporates IV)
- Filters out truly worthless options

**Cons:**
- Requires delta data — TheoPrice stream must be active, or compute via Black-Scholes
- Delta changes with underlying price and time — need live recalculation
- For 0DTE, delta near ATM is ~0.50, far OTM is ~0.001 — threshold is sensitive
- Need to handle edge cases (delta unavailable at subscription time)

---

### Option C: Percentage Distance from ATM

Select strikes within ±X% of the current underlying price.

```yaml
watchlist:
  tickers:
    - symbol: "SPY"
      strike_filter:
        mode: "percent"
        percent: 3.0        # ±3% of underlying price
```

**Pros:**
- Intuitive — directly maps to "how far from the money"
- Fixed, predictable coverage
- No delta dependency

**Cons:**
- Doesn't adapt to IV (high IV → 3% is too tight; low IV → 3% is too wide)
- Doesn't account for intraday moves (need to refresh or set wide)
- Different underlyings have different vol regimes — one size doesn't fit all

---

### Option D: Hybrid — Percentage + Delta Floor (RECOMMENDED)

Combine a wide percentage band with a delta floor, refreshed intraday.

```yaml
watchlist:
  tickers:
    - symbol: "SPY"
      strike_filter:
        mode: "hybrid"
        percent: 5.0           # ±5% initial band
        min_abs_delta: 0.02    # exclude |delta| < 0.02
        refresh_interval_min: 60  # re-evaluate ATM center hourly
```

**Logic:**
1. At subscription time, fetch underlying price
2. Select all strikes within ±5% of price
3. From those, exclude strikes where TheoPrice reports `|delta| < 0.02` (or BS-estimated delta)
4. Every 60 minutes, re-evaluate the underlying price and re-filter

**Pros:**
- Percentage band captures the economically active range
- Delta floor filters out the truly dead strikes within that band
- Adapts to intraday moves via refresh
- Predictable symbol count (~100–150 per underlying)
- Graceful degradation if TheoPrice unavailable (fall back to BS delta)

**Cons:**
- More complex implementation
- Two parameters to tune (percent + delta)
- Refresh requires subscribe/unsubscribe (already supported by DynamicStrikeManager)

---

### Option E: Volume/OI-Based Filter

Select strikes with open interest ≥ N or volume > 0.

```yaml
watchlist:
  tickers:
    - symbol: "SPY"
      strike_filter:
        mode: "activity"
        min_open_interest: 10
```

**Pros:**
- Directly targets "traded" strikes
- No parameter tuning needed

**Cons:**
- Requires chain data with OI/volume (SDK provides this, but adds API latency)
- OI is stale (end-of-day snapshot) — doesn't reflect intraday activity
- New strikes have no OI yet — would be excluded
- Doesn't work for 0DTE at market open (all OI is from previous day)

---

## 5. Recommendation: Option D (Hybrid)

**Recommended approach: Hybrid (Percentage Band + Delta Floor)**

Rationale:
1. **Percentage band** (±4–5%) captures the economically active range where >99% of 0DTE volume occurs
2. **Delta floor** (|delta| ≥ 0.02) filters out the dead strikes within that band that have no bid and no trades
3. **Intraday refresh** (every 60 min via DynamicStrikeManager) adapts to underlying price moves
4. **Fallback to Black-Scholes delta** when TheoPrice is unavailable ensures robustness
5. **Predictable symbol budget**: ~100–150 strikes per underlying → ~300–450 total for SPY+SPX+/ES, well within DXLink limits

### Parameter Defaults

| Underlying | Percent Band | Min |Delta| | Expected Strikes |
|---|---|---|---|---|
| SPY | ±4% | 0.02 | ~80–120 |
| SPX | ±5% | 0.02 | ~100–150 |
| /ES | ±5% | 0.02 | ~100–150 |
| **Total** | — | — | **~280–420** |

This leaves headroom for additional underlyings (QQQ, IWM) or wider bands.

---

## 6. Design

### 6.1 Config Schema

Add `StrikeFilterConfig` to `TickerConfig`:

```python
class StrikeFilterConfig(BaseModel):
    """Strike filtering configuration for option chain subscription.
    
    Reduces the number of subscribed symbols by excluding far OTM strikes
    that have no trade activity, staying within DXLink subscription limits.
    """
    mode: str = Field(
        default="hybrid",
        pattern=r"^(none|count|delta|percent|hybrid)$",
    )
    # Count mode
    count: int = Field(default=50, ge=1, le=500)
    # Percent mode / Hybrid band
    percent: float = Field(default=4.0, gt=0, le=50)
    # Delta mode / Hybrid floor
    min_abs_delta: float = Field(default=0.02, ge=0, le=1)
    # Intraday refresh
    refresh_interval_min: int = Field(default=60, ge=5, le=240)


class TickerConfig(BaseModel):
    symbol: str
    option_type: str = Field(default="equity", pattern=r"^(equity|futures)$")
    expiration_filter: str = Field(default="0DTE", pattern=r"^(0DTE|all)$")
    strike_filter: StrikeFilterConfig = Field(default_factory=StrikeFilterConfig)
    alert_rules: list[CelAlertRule] = Field(default_factory=list)
    underlying_alert_rules: list[CelAlertRule] = Field(default_factory=list)
```

### 6.2 YAML Configuration

```yaml
watchlist:
  tickers:
    - symbol: "SPY"
      option_type: "equity"
      expiration_filter: "0DTE"
      strike_filter:
        mode: "hybrid"
        percent: 4.0
        min_abs_delta: 0.02
        refresh_interval_min: 60

    - symbol: "SPX"
      option_type: "equity"
      expiration_filter: "0DTE"
      strike_filter:
        mode: "hybrid"
        percent: 5.0
        min_abs_delta: 0.02
        refresh_interval_min: 60

    - symbol: "/ES"
      option_type: "futures"
      expiration_filter: "0DTE"
      strike_filter:
        mode: "hybrid"
        percent: 5.0
        min_abs_delta: 0.02
        refresh_interval_min: 60
```

### 6.3 Filtering Logic

New module: `src/dxlink_scanner/strike_filter.py`

```python
"""Strike filtering to reduce DXLink subscription count."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from dxlink_scanner.config import StrikeFilterConfig
from dxlink_scanner.models import StrikeInfo


@dataclass(slots=True)
class FilterResult:
    """Result of applying a strike filter."""
    included: list[StrikeInfo]
    excluded: list[StrikeInfo]
    underlying_price: Decimal | None


def filter_strikes(
    strikes: list[StrikeInfo],
    config: StrikeFilterConfig,
    underlying_price: Decimal | None,
    delta_map: dict[str, Decimal] | None = None,
) -> FilterResult:
    """Filter strikes based on the configured mode.
    
    Args:
        strikes: All strikes from the option chain.
        config: Strike filter configuration.
        underlying_price: Current underlying price (for percent/count modes).
        delta_map: Optional map of streamer_symbol → delta (for delta/hybrid modes).
    
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
        return FilterResult(included=strikes[:count], excluded=strikes[count:],
                            underlying_price=underlying_price)
    
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
        return FilterResult(included=strikes, excluded=[], underlying_price=None)
    
    band = underlying_price * Decimal(str(percent / 100))
    included = [s for s in strikes if abs(s.strike - underlying_price) <= band]
    excluded = [s for s in strikes if abs(s.strike - underlying_price) > band]
    return FilterResult(included=included, excluded=excluded,
                        underlying_price=underlying_price)


def _filter_by_delta(
    strikes: list[StrikeInfo],
    min_abs_delta: float,
    delta_map: dict[str, Decimal] | None,
) -> FilterResult:
    """Select strikes where |delta| >= threshold."""
    if delta_map is None:
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
    # Step 1: Apply percent band
    band_result = _filter_by_percent(strikes, config.percent, underlying_price)
    
    # Step 2: Apply delta floor within the band
    if delta_map is None:
        return band_result
    
    min_d = Decimal(str(config.min_abs_delta))
    included = []
    excluded = band_result.excluded  # Already excluded by percent band
    
    for s in band_result.included:
        d = delta_map.get(s.symbol)
        if d is not None and abs(d) >= min_d:
            included.append(s)
        else:
            excluded.append(s)
    
    return FilterResult(included=included, excluded=excluded,
                        underlying_price=underlying_price)
```

### 6.4 Integration Points

#### A. `bootstrap.py` — `parse_chain()` enhancement

Add optional `strike_filter` and `underlying_price` parameters to `parse_chain()`:

```python
def parse_chain(
    chains: dict[date, list[Option | FutureOption]],
    expiration_filter: str = "0DTE",
    is_future: bool = False,
    strike_filter: StrikeFilterConfig | None = None,
    underlying_price: Decimal | None = None,
    delta_map: dict[str, Decimal] | None = None,
) -> tuple[UnderlyingInfo, list[StrikeInfo]]:
    """Parse SDK option chain result into typed structures.
    
    When strike_filter is provided, applies filtering to reduce the
    number of returned strikes for DXLink subscription efficiency.
    """
    # ... existing logic to extract all strikes ...
    
    if strike_filter is not None and strike_filter.mode != "none":
        from dxlink_scanner.strike_filter import filter_strikes
        result = filter_strikes(strikes, strike_filter, underlying_price, delta_map)
        logger.info(
            "Strike filter %s: %d included, %d excluded (price=%s)",
            strike_filter.mode, len(result.included), len(result.excluded),
            underlying_price,
        )
        strikes = result.included
    
    return underlying_info, strikes
```

#### B. `cli.py` — Wire filter config from TickerConfig

In `_run_scanner()`, pass `ticker.strike_filter` to `parse_chain()`:

```python
underlying_info, strikes = loader.parse_chain(
    chains,
    ticker.expiration_filter,
    is_future=ticker.option_type == "futures",
    strike_filter=ticker.strike_filter,
    underlying_price=underlying_price,
    delta_map=None,  # Populated after TheoPrice subscription
)
```

#### C. `dynamic_strikes.py` — Respect filter on rescan

Update `_collect_symbols()` to apply the filter:

```python
async def _collect_symbols(self, ticker):
    chains = await self._loader.get_nested_chain(ticker.symbol, ticker.option_type)
    underlying_info, strikes = self._loader.parse_chain(
        chains,
        ticker.expiration_filter,
        is_future=ticker.option_type == "futures",
        strike_filter=ticker.strike_filter,
        underlying_price=await self._loader.get_underlying_price(
            ticker.symbol, ticker.option_type
        ),
    )
    symbols = [s.symbol for s in strikes]
    return ticker.symbol, symbols, underlying_info.symbol
```

#### D. Delta enrichment (Phase 2)

For the delta floor in hybrid mode, we need delta values. Two sources:

1. **TheoPrice stream** (live): Subscribe first, collect deltas for 30s, then apply filter
2. **Black-Scholes estimate** (fallback): Compute from strike, expiry, and underlying price

```python
# In cli.py, after initial TheoPrice collection:
delta_map: dict[str, Decimal] = {}
for sym, snap in store._snapshots.items():
    if snap.delta is not None:
        delta_map[sym] = snap.delta

# Re-apply filter with delta data
if ticker.strike_filter.mode in ("delta", "hybrid"):
    underlying_info, strikes = loader.parse_chain(
        chains,
        ticker.expiration_filter,
        is_future=ticker.option_type == "futures",
        strike_filter=ticker.strike_filter,
        underlying_price=underlying_price,
        delta_map=delta_map,
    )
```

### 6.5 Logging & Observability

```python
# Log filter results at INFO level
logger.info(
    "Strike filter [%s] mode=%s: %d strikes → %d included, %d excluded (%.1f%% reduction)",
    ticker.symbol,
    config.mode,
    len(all_strikes),
    len(included),
    len(excluded),
    len(excluded) / len(all_strikes) * 100 if all_strikes else 0,
)
```

### 6.6 Backward Compatibility

- Default `mode: "hybrid"` with `percent: 4.0` and `min_abs_delta: 0.02`
- `mode: "none"` preserves current behavior (subscribe to all strikes)
- Existing configs without `strike_filter` get sensible defaults via Pydantic
- No breaking changes to `TickerConfig` — new field has a default

---

## 7. Sprint Breakdown

### Sprint 1: Core Filter Logic (2 days)

**Goal**: Implement `strike_filter.py` with all filter modes and unit tests.

| Task | Description | Deliverable |
|---|---|---|
| 1.1 | Create `StrikeFilterConfig` Pydantic model in `config/__init__.py` | Config schema |
| 1.2 | Implement `strike_filter.py` with `filter_strikes()` and all modes | Filter module |
| 1.3 | Unit tests for each filter mode (count, percent, delta, hybrid, none) | `tests/test_strike_filter.py` |
| 1.4 | Edge cases: empty strikes, no underlying price, missing delta_map | Test coverage |

**Acceptance**: All filter modes produce correct included/excluded lists. 100% branch coverage.

---

### Sprint 2: Bootstrap Integration (2 days)

**Goal**: Wire filter into `parse_chain()` and `cli.py`.

| Task | Description | Deliverable |
|---|---|---|
| 2.1 | Add `strike_filter` and `delta_map` params to `parse_chain()` | Updated bootstrap.py |
| 2.2 | Update `cli.py:_run_scanner()` to pass filter config | Updated cli.py |
| 2.3 | Update `dynamic_strikes.py` to pass filter on rescan | Updated dynamic_strikes.py |
| 2.4 | Integration test: full chain → filter → subscription count | `tests/test_filter_integration.py` |

**Acceptance**: Scanner starts with filtered strike count. Logs show reduction percentage.

---

### Sprint 3: Delta Enrichment & Hybrid Mode (2 days)

**Goal**: Enable delta floor in hybrid mode using TheoPrice data.

| Task | Description | Deliverable |
|---|---|---|
| 3.1 | Collect initial TheoPrice deltas before filter application | Delta bootstrap |
| 3.2 | Implement BS delta fallback when TheoPrice unavailable | `_black_scholes_delta()` reuse |
| 3.3 | Two-phase subscription: subscribe all → collect deltas → filter → unsubscribe excess | Phased startup |
| 3.4 | Test delta floor excludes far OTM correctly | Test coverage |

**Acceptance**: Hybrid mode produces correct results with both TheoPrice and BS delta.

---

### Sprint 4: Intraday Refresh & Monitoring (1 day)

**Goal**: Re-apply filter on DynamicStrikeManager rescan and add observability.

| Task | Description | Deliverable |
|---|---|---|
| 4.1 | Update rescan to re-evaluate filter with latest underlying price | Refresh logic |
| 4.2 | Log filter statistics (included/excluded/reduction %) on each rescan | Logging |
| 4.3 | Add `strike_filter_reduction` metric to stats logger | Observability |
| 4.4 | End-to-end test: simulate underlying move → rescan → filter update | Integration test |

**Acceptance**: Filter adapts to intraday moves. Logs show filter stats every 60 min.

---

### Sprint 5: Documentation & Polish (1 day)

**Goal**: Document the feature and update all references.

| Task | Description | Deliverable |
|---|---|---|
| 5.1 | Update `docs/configuration.md` with `strike_filter` reference | Config docs |
| 5.2 | Update `docs/api_reference.md` with new models | API docs |
| 5.3 | Update `docs/index.md` example config | Index docs |
| 5.4 | Update `production.yaml` with recommended filter settings | Config template |
| 5.5 | Update `ROADMAP.md` — mark Phase 2 as in-progress | Roadmap |

**Acceptance**: All docs reflect the new feature. Example config is copy-paste ready.

---

## 8. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| TheoPrice unavailable at startup | Delta floor can't be applied | Fall back to BS delta estimate; log warning |
| Underlying moves beyond filter band | Miss trades on newly-ATM strikes | Refresh every 60 min; set band wide enough (±5%) |
| Filter too aggressive | Miss unusual activity on wing strikes | Monitor excluded strikes via logging; tune parameters |
| DXLink still over limit after filter | Subscription fails | Add `max_symbols` hard cap; log error with actionable message |
| Delta staleness | Filter uses outdated delta | Re-apply filter on rescan with fresh TheoPrice data |

---

## 9. Success Metrics

| Metric | Target | Measurement |
|---|---|---|
| Subscribed symbols per underlying | ≤ 150 | `len(all_symbols)` in startup log |
| Total subscribed symbols (SPY+SPX+/ES) | ≤ 450 | Sum across tickers |
| Filter reduction rate | ≥ 60% | `(total - included) / total * 100` |
| Missed actionable trades | 0 | Compare filtered vs unfiltered on replay data |
| DXLink subscription success | 100% | No subscription errors in logs |
| Memory footprint reduction | ≥ 50% | `SnapshotStore` size comparison |

---

## 10. Future Enhancements

- **Adaptive percent band**: Adjust band width based on realized vol (wider in high vol)
- **Per-regime filtering**: Wider band in crash regime (tail risk monitoring)
- **Dynamic count**: Adjust count based on remaining DXLink budget
- **Excluded strike monitoring**: Track if excluded strikes suddenly become active (signal to widen filter)
- **Multi-connection splitting**: If single connection insufficient, split across multiple DXLink connections
