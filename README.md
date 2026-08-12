# DXLink Scanner — Real-Time Options Volume Scanner

Multi-ticker options volume scanner using Tastytrade DXLink streaming, powered by a CEL-based rule engine.

## Features

- **Multi-ticker support**: Watch SPY, QQQ, IWM, SPX, ES, NQ, and more in a single instance
- **Delta-based symbol filtering**: Subscribe to all 0DTE symbols, filter by delta, unsubscribe non-qualifying symbols (`delta_filter: true` + `min_delta`)
- **CEL rule engine**: Define alert rules as CEL expressions in YAML config — per-symbol, per-underlying, and default fallback tiers
- **Per-underlying scoping**: Define one rule that applies to ALL option strikes of an underlying (e.g., SPY calls, SPX puts)
- **Equity and futures options**: Supports both stock/ETF options (SPY, QQQ) and futures options (ES, NQ)
- **Extended TimeAndSale fields**: bidPrice, askPrice, trade type via DXLink COMPACT format
- **Debug mode**: `--debug-messages` flag logs every raw WebSocket message for protocol troubleshooting
- **DXLink WebSocket**: TARDIS protocol with SETUP → AUTH → CHANNEL_REQUEST → FEED_SETUP → FEED_SUBSCRIPTION
- **Alert sinks**: JSONL stdout output and webhook delivery with retry/backoff

## Supported Instrument Types

| `option_type` | Underlying examples | API endpoint |
|---|---|---|
| `equity` | SPY, QQQ, IWM, SPX, AAPL, TSLA | `GET /option-chains/{symbol}/nested` |
| `futures` | ES, NQ, CL, GC (futures product codes) | `GET /futures-option-chains/{symbol}/nested` |

## Prerequisites
- Tastytrade API credentials (client_id, client_secret, refresh_token)
- See `.env.example` for required environment variables

## Setup
```bash
uv sync
cp .env.example .env  # fill in your Tastytrade credentials
```

## Configuration

Configure multiple tickers with per-ticker CEL rules in `production.yaml`:

```yaml
watchlist:
  # Global fallback rules (apply when no per-symbol/underlying rule matches)
  default_alert_rules:
    - name: "absolute_size"
      expression: "trade.size >= 1000"
      severity: "critical"

  tickers:
    # - symbol: "SPY"
    #   option_type: "equity"
    #   strikes_around_atm: 10
    #   delta_filter: true           # Enable delta-based filtering
    #   min_delta: 0.02              # Subscribe to options with |delta| >= 0.02
    #   expiration_filter: "0DTE"
    #   # Applies to ALL SPY option symbols
    #   underlying_alert_rules:
    #     - name: "large_option_print"
    #       expression: "trade.is_option && trade.size >= 100 && trade.price > 1.0"
    #       severity: "high"
    #
    #   # When delta_filter=true, the scanner subscribes to TheoPrice for all
    #   # 0DTE symbols, filters by min_delta, then unsubscribes non-qualifying
    #   # symbols. Only Quote and TAS are subscribed for the final symbol set.
    #   # When delta_filter=false (default), uses strikes_around_atm instead.

    - symbol: "SPX"
      option_type: "equity"
      strikes_around_atm: 20
      expiration_filter: "0DTE"
      underlying_alert_rules:
        - name: "large_spx_print"
          expression: "trade.is_option && trade.size >= 50"
          severity: "high"

    - symbol: "/ES"
      option_type: "futures"
      strikes_around_atm: 10
      expiration_filter: "0DTE"
      underlying_alert_rules:
        - name: "large_es_print"
          expression: "trade.is_option && trade.size >= 2 && trade.price > 2.0"
          severity: "high"
```

See [docs/cel_rules.md](docs/cel_rules.md) for the full CEL rule reference.

### Rule Evaluation Order

1. **`alert_rules`** — exact match on the streamer symbol (e.g. `.SPY260731C500`)
2. **`underlying_alert_rules`** — resolved from the underlying of an option symbol (e.g. `.SPY260731C500` → `SPY`)
3. **`default_alert_rules`** — global fallback

First match wins within each tier.

### Delta-Based Symbol Filtering

When `delta_filter: true` is set on a ticker, the scanner uses TheoPrice to determine each option's delta at bootstrap, then subscribes only to options whose absolute delta exceeds `min_delta`. This automatically excludes far-out-of-the-money options that rarely produce meaningful volume.

**Workflow**:
1. Fetch 0DTE option chain for the underlying
2. Subscribe to `TheoPrice` for all 0DTE symbols (buffers 2-5 seconds)
3. Filter out symbols where `|delta| < min_delta`
4. Unsubscribe filtered symbols from TheoPrice
5. Subscribe remaining symbols to `Quote` and `TimeAndSale`

**Config**:
```yaml
watchlist:
  tickers:
    - symbol: "SPY"
      option_type: "equity"
      delta_filter: true     # Enable delta-based filtering
      min_delta: 0.02        # Only subscribe to |delta| >= 0.02
      strikes_around_atm: 20 # Fallback if delta filter fails
      expiration_filter: "0DTE"
```

Without `delta_filter` (default `false`), the scanner uses `strikes_around_atm` to select symbols — a simpler approach that subscribes to N strikes around the current ATM.

## Run
```bash
uv run python -m dxlink_scanner.cli --config production.yaml --verbose
# Enable Parquet persistence by setting persist_events: true in production.yaml
uv run python -m dxlink_scanner.cli --config production.yaml --verbose --debug-messages  # log every WebSocket message
# or
uv run dxlink-scanner --config production.yaml --debug-messages  # console script entry point
```

## Test
```bash
uv run pytest tests/ -v
```

## Lint / Typecheck
```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
```
