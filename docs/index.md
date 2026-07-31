# Options Radar Zero Scanner — Documentation

**Real-time options volume scanner using Tastytrade DXLink streaming**

## Quick Links

| Topic | Document |
|-------|----------|
| 🏗️ System Architecture | [architecture.md](architecture.md) |
| ⚙️ Configuration Reference | [configuration.md](configuration.md) |
| 💾 Data Files & Storage | [data_files.md](data_files.md) |
| 🔮 CEL Rule Engine | [cel_rules.md](cel_rules.md) |
| 🚀 Deployment & Operations | [deployment.md](deployment.md) |
| 📚 API Reference | [api_reference.md](api_reference.md) |
| 📋 Implementation Plan | [implementation_plan.md](implementation_plan.md) |
| 🔧 Consolidated Event Model | [consolidated_event_model.md](consolidated_event_model.md) |

## Overview

The **DXLink Scanner** is a real-time options volume scanner that connects to Tastytrade's DXLink WebSocket feed to monitor 0DTE (zero days to expiration) options for configurable underlying symbols (SPY, QQQ, SPX, ES, etc.).

### Key Features

- **Real-time streaming**: Connects to DXLink WebSocket for Quote and TimeAndSale events
- **Consolidated event model**: Merges both event types into a per-symbol snapshot for cross-message analytics
- **Underlying price**: Derived from Quote mid_price (bid+ask)/2 on the underlying streamer symbol
- **CEL rule engine**: CEL-based rules with three tiers — per-symbol, underlying-scoped, and default fallback
- **Per-underlying scoping**: Define one rule that applies to ALL option strikes of an underlying
- **Parquet persistence**: Events written to partitioned Parquet files for historical analysis
- **Multiple outputs**: JSON lines to stdout, webhook delivery with retry logic
- **Graceful shutdown**: SIGTERM handling with Parquet flush on market close

### Supported Instruments

| Type | Examples | Notes |
|------|----------|-------|
| Equity options (0DTE) | SPY, QQQ, IWM, DIA | Same-day expiry only |
| Index options | SPX, NDX, RUT | Cash-settled, European |
| Futures options | ES, NQ, RTY | `/ES:XCME` format |
| Underlying futures | /ES, /NQ, /RTY | Quote subscription supported |

### Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.14+ |
| Package Manager | uv |
| Streaming | tastytrade SDK (`DXLinkStreamer`) |
| Serialization | orjson, PyArrow |
| Config | Pydantic + YAML |
| Rule Engine | cel-expr-python (CEL) |
| Testing | pytest, pytest-asyncio, pytest-cov |
| Linting | ruff |
| Type Checking | mypy (strict) |

## Quick Start

```bash
# Install dependencies
uv sync

# Copy config template
cp production.yaml.example production.yaml
# Edit production.yaml with your Tastytrade credentials

# Run scanner
uv run dxlink-scanner --config production.yaml
```

## Project Structure

```
scanner/
├── docs/                    # Documentation (this directory)
├── src/dxlink_scanner/
│   ├── cli.py              # CLI entry point, main event loop
│   ├── config/__init__.py  # Pydantic config models
│   ├── models.py           # Data models (Alert, TimeAndSaleEvent, snapshots)
│   ├── rules/
│   │   ├── cel_engine.py   # CEL-based rule engine
│   │   └── __init__.py     # Exports CELRuleEngine
│   ├── sinks/
│   │   ├── stdout_sink.py  # JSON lines output
│   │   ├── webhook_sink.py # HTTP webhook with retry
│   │   └── __init__.py
│   ├── stats/
│   │   ├── rolling_v2.py    # RollingStatsManagerV2 / RollingStatsV2 (median/MAD/percentiles/time decay)
│   │   └── __init__.py
│   ├── schemas/
│   │   ├── v1.py           # ConsolidatedEvent Parquet schema v1
│   │   └── v2.py           # Schema v2 with derived Quote fields (mid_price, spread, etc.)
│   ├── snapshot_store.py   # In-memory snapshot + Parquet flush
│   ├── dynamic_strikes.py  # Dynamic strike management
│   ├── auth.py             # Tastytrade authentication
│   ├── bootstrap.py        # Option chain loading
│   ├── debug_dxfeed.py     # DXLink debugging utility
│   └── poc_tt.py           # Tastytrade API proof-of-concept
├── tests/                  # Test suite (116 tests)
├── production.yaml         # Production config template
├── dxlink-dxlink_scanner.yaml     # Example config
└── pyproject.toml          # Project metadata & tool config
```

## Configuration

The scanner uses a YAML configuration file with environment variable substitution (`${VAR}` pattern). See [configuration.md](configuration.md) for full reference.

### Minimal Config Example

```yaml
tastytrade:
  client_id: ${TASTY_CLIENT_ID}
  client_secret: ${TASTY_CLIENT_SECRET}
  refresh_token: ${TASTY_REFRESH_TOKEN}
  sandbox: false

watchlist:
  tickers:
    - symbol: "SPY"
      option_type: "equity"
      strikes_around_atm: 10
      underlying_alert_rules:
        - name: "large_print"
          expression: "trade.is_option && trade.size >= 50 && trade.price > 1.0"
          severity: "high"

detection:
  size_mult: 5.0
  abs_min_size: 10

outputs:
  stdout: true
  persist_events: true
  webhook:
    enabled: false
    url: "http://localhost:8080/alerts"
```

## Running

### Development

```bash
# Run with production config
uv run dxlink-scanner --config production.yaml

# Enable event persistence to Parquet (also set persist_events: true in config)
uv run dxlink-scanner --config production.yaml  # persistence enabled via persist_events: true in config

# Debug raw DXLink messages
uv run dxlink-scanner --config production.yaml --debug-messages
```

### Production

```bash
# Use production config (with env var substitution)
uv run dxlink-scanner --config production.yaml  # persistence enabled via persist_events: true in config-events
# or via config: persist_events: true
```

## Testing

```bash
# Run all tests with coverage
uv run pytest tests/ -v

# Run with coverage report
uv run pytest tests/ --cov=src --cov-report=term-missing

# Type check
uv run mypy src/

# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Data Flow

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Tastytrade     │────▶│  DXLinkStreamer  │────▶│  Unified        │
│  DXLink WS      │     │  (2 producers)   │     │  Consumer       │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Alert Sinks    │◀────│  Rule Engines    │◀────│  SnapshotStore  │
│  (stdout, web)  │     │  CELRuleEngine   │     │  (mem + Parquet)│
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

See [architecture.md](architecture.md) for detailed process flows.

## License

Internal use only.
