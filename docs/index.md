# DXLink Scanner — Documentation

**Real-time 0DTE options volume scanner using Tastytrade DXLink streaming**

## Quick Links

| Topic | Document |
|-------|----------|
| Architecture | [architecture.md](architecture.md) |
| Configuration Reference | [configuration.md](configuration.md) |
| CEL Rule Engine | [cel_rules.md](cel_rules.md) |
| Statistical Models | [model_cards.md](model_cards.md) |
| DXLink Chunking Design | [dxlink_chunking_design.md](dxlink_chunking_design.md) |
| DXLink Chunking Spec | [dxlink_chunking_spec.md](dxlink_chunking_spec.md) |
| Filtering Research | [filtering.md](filtering.md) |

## Overview

The **DXLink Scanner** is a real-time 0DTE options volume scanner that connects to Tastytrade's DXLink WebSocket feed to monitor options for configurable underlying symbols (SPY, QQQ, SPX, /ES).

### Key Features

- **Real-time streaming**: Connects to DXLink WebSocket for Quote, TimeAndSale, and TheoPrice events
- **Statistical anomaly detection**: Bayesian Gamma-Poisson, Hawkes process, regime detection, VPIN, VAP, seasonality
- **Adaptive tuning**: Feedback loop adjusts thresholds based on realized FDR/TPR
- **Delta drift monitoring**: Local Black-Scholes delta vs DXLink delta comparison
- **DXLink payload chunking**: Automatic chunking to stay under 64k message limit
- **Decision-theoretic alerting**: Cost-aware rules with online FDR control
- **CEL rule engine**: Three tiers — per-symbol, underlying-scoped, and default fallback
- **Parquet persistence**: Events written to partitioned Parquet files for historical analysis
- **Multiple outputs**: JSON lines to stdout, webhook delivery with retry logic

### Supported Instruments

| Type | Examples | Notes |
|------|----------|-------|
| Equity options (0DTE) | SPY, QQQ | Same-day expiry only |
| Index options | SPX | Cash-settled, European |
| Futures options | /ES, /NQ | `/ES:XCME` format |

### Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.14+ |
| Package Manager | uv |
| Streaming | tastytrade SDK + custom chunked wrapper |
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
dxlink-scanner/
├── src/dxlink_scanner/
│   ├── cli.py                  # CLI entry point, main event loop
│   ├── config/__init__.py      # Pydantic config models
│   ├── models.py               # Data models (Alert, TimeAndSaleEvent, snapshots)
│   ├── chunked_streamer.py     # DXLink payload chunking wrapper
│   ├── rules/
│   │   └── cel_engine.py       # CEL-based rule engine
│   ├── sinks/
│   │   ├── stdout_sink.py      # JSON lines output
│   │   └── webhook_sink.py     # HTTP webhook with retry
│   ├── stats/
│   │   ├── statistical_analysis.py  # Bayesian, Hawkes, Seasonality, Regime, VAP
│   │   ├── model_store.py      # Model persistence, prior elicitation
│   │   ├── dynamic_thresholds.py  # DynamicThresholdManager, AdaptiveTuner
│   │   ├── microstructure.py   # VPIN, FlowMetrics, CrossAssetHawkes
│   │   ├── rolling_v2.py       # RollingStatsManagerV2
│   │   └── seasonality.py      # TimeOfDayAggregator
│   ├── schemas/
│   │   ├── v1.py               # Parquet schema v1
│   │   └── v2.py               # Parquet schema v2
│   ├── snapshot_store.py       # In-memory snapshot + Parquet flush
│   ├── dynamic_strikes.py      # Dynamic strike management
│   ├── auth.py                 # Tastytrade authentication
│   ├── bootstrap.py            # Option chain loading
│   └── debug_dxfeed.py         # DXLink debugging utility
├── tests/                      # Test suite (309 tests)
├── docs/                       # Documentation
├── production.yaml             # Production config
└── pyproject.toml              # Project metadata
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
      expiration_filter: "0DTE"
      underlying_alert_rules:
        - name: "large_print"
          expression: "trade.is_option && trade.size >= 50 && trade.price > 1.0"
          severity: "high"

detection:
  size_mult: 2.0
  abs_min_size: 5
  fdr_alpha: 0.05
  vpin_threshold: 0.6

dxlink:
  max_payload_bytes: 60000
  chunk_delay_sec: 0.1
  enable_chunking: true

outputs:
  stdout: true
  persist_events: true
  data_dir: "data/events"
  webhook:
    enabled: false
    url: "http://localhost:8080/alerts"
```

## Running

### Development

```bash
# Run with production config
uv run dxlink-scanner --config production.yaml

# Debug raw DXLink messages
uv run dxlink-scanner --config production.yaml --debug-messages
```

### Production

```bash
# Use production config (with env var substitution)
uv run dxlink-scanner --config production.yaml
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
```

## Data Flow

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Tastytrade     │────▶│  ChunkedDXLink   │────▶│  Unified        │
│  DXLink WS      │     │  Streamer        │     │  Consumer       │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Alert Sinks    │◀────│  CEL Rule Engine │◀────│  SnapshotStore  │
│  (stdout, web)  │     │  + Stats Models  │     │  (mem + Parquet)│
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

See [architecture.md](architecture.md) for detailed process flows.

## Stats Pipeline

| Model | Module | Purpose |
|-------|--------|---------|
| Bayesian Gamma-Poisson | `statistical_analysis.py` | Trade count posterior, anomaly scoring |
| Hawkes Process | `statistical_analysis.py` | Self-exciting trade clustering |
| Time-of-Day Seasonality | `statistical_analysis.py` | Intraday volume pattern normalization |
| Cross-Symbol Pooling | `statistical_analysis.py` | Hierarchical Bayes for sparse symbols |
| Volume-at-Price | `statistical_analysis.py` | POC, value area, imbalance |
| Regime Detector | `statistical_analysis.py` | Volatility regime classification |
| VPIN Calculator | `microstructure.py` | Order flow toxicity |
| Flow Metrics | `microstructure.py` | Liquidity, trade classification |
| Cross-Asset Hawkes | `microstructure.py` | Systemic flow detection |
| Dynamic Thresholds | `dynamic_thresholds.py` | Expression-based thresholds |
| Adaptive Tuner | `dynamic_thresholds.py` | FDR/TPR feedback loop |

## License

Internal use only.