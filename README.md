# DXLink Scanner — Real-Time Options Volume Scanner

Multi-ticker options volume scanner using Tastytrade DXLink streaming, powered by a CEL-based rule engine.

## Features

- **Multi-ticker support**: Watch SPY, QQQ, IWM, SPX, ES, NQ, and more in a single instance
- **ATM strike selection**: Subscribe to N strikes around ATM for 0DTE options
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