# Roadmap

## Phase 1: Core Features ✅

## Phase 2: Dynamic Options Selection (Planned)

### Delta-Based Strike Selection
Currently uses a fixed number of strikes from ATM (e.g., 10 ATM ± 5).
- **Goal**: Switch to delta-based selection (e.g., strikes where `|delta| >= 0.01`)
- **Benefits**: 
  - Adapts to IV changes automatically
  - Economically meaningful coverage (non-zero price options)
  - Better for low-strike/OTM options
- **Tradeoff**: Requires delta data from option chain
- **Hybrid option**: ATM-center + delta filter ("10 strikes from ATM that also have delta ≥ 0.01")

### Intraday Option Selection Reset
Currently loads 0DTE strikes once at startup.
- **Goal**: Re-evaluate and expand watchlist intraday
- **Proposed schedule**:
  - **09:30 ET (open)**: Load today's 0DTE strikes (ATM ± N)
  - **12:00 ET (midday)**: Refresh + expand range (underlying may have moved significantly)
  - **15:00 ET (close)**: Tighten back to ATM ± N (focus on liquidity near close)

## Phase 3: RTH Session Analytics

### RTH Open / High / Low Tracking
Track per-symbol RTH open, high, and low prices from TAS events.

**Implementation**: Add `rth_open`, `rth_high`, `rth_low` to `ConsolidatedSnapshot` or `RollingStatsV2`. Update on each RTH TAS event:

```python
if rth_is_open(timestamp) and not snap.rth_open:
    snap.rth_open = trade_price
snap.rth_high = max(snap.rth_high or 0, trade_price)
snap.rth_low = min(snap.rth_low or float('inf'), trade_price)
```

**CEL variables exposed**:
- `rth.open` — RTH session open price (first trade 09:30-10:00 ET)
- `rth.high` — RTH session high price
- `rth.low` — RTH session low price

**Example rules**:
```cel
# Alert when trade crosses above RTH open
trade.price > rth.open

# Alert when trade sets new RTH high
trade.price > rth.high

# Alert when trade sets new RTH low
trade.price < rth.low

# Alert when price breaks RTH range
trade.price > rth.high && trade.size >= 100  # breakout
```

**Reset**: Clear at 09:30 ET daily (start of new RTH session).

## Phase 4: Post-MVP

### DXLink Reconnection
- Exponential backoff + resubscribe
- Handle session timeouts gracefully

### Horizontal Scaling
- Multiple DXLink connections via separate accounts
- Redis-backed SnapshotStore for cross-instance state

### Metrics & Observability
- Prometheus exporter
- Health check endpoints
