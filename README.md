# DXLink Scanner — Real-Time Options Volume Scanner

Multi-ticker 0DTE options volume scanner using Tastytrade DXLink streaming, powered by a CEL-based rule engine with statistical anomaly detection.

## Features

- **Multi-ticker support**: Watch SPY, QQQ, SPX, /ES in a single instance
- **Full 0DTE chain subscription**: Subscribe to all 0DTE strikes per underlying
- **CEL rule engine**: Define alert rules as CEL expressions in YAML config — per-symbol, per-underlying, and default fallback tiers
- **Statistical anomaly detection**: Bayesian Gamma-Poisson, Hawkes process, regime detection, VPIN, VAP, seasonality
- **Adaptive tuning**: Feedback loop adjusts thresholds based on realized FDR/TPR
- **Delta drift monitoring**: Local Black-Scholes delta vs DXLink delta comparison
- **DXLink payload chunking**: Automatic chunking to stay under 64k message limit
- **Decision-theoretic alerting**: Cost-aware rules with online FDR control
- **Per-underlying scoping**: Define one rule that applies to ALL option strikes of an underlying
- **Equity and futures options**: Supports both stock/ETF options (SPY, QQQ) and futures options (/ES)
- **Debug mode**: `--debug-messages` flag logs every raw WebSocket message
- **Alert sinks**: JSONL stdout output and webhook delivery with retry/backoff

## Supported Instrument Types

| `option_type` | Underlying examples | API endpoint |
|---|---|---|
| `equity` | SPY, QQQ, SPX | `GET /option-chains/{symbol}/nested` |
| `futures` | /ES, /NQ | `GET /futures-option-chains/{symbol}/nested` |

## Prerequisites

- Tastytrade API credentials (client_id, client_secret, refresh_token)
- Set env vars: `TASTY_CLIENT_ID`, `TASTY_CLIENT_SECRET`, `TASTY_REFRESH_TOKEN`, `WEBHOOK_URL`

## Setup

```bash
uv sync
```

## Configuration

Configure multiple tickers with per-ticker CEL rules in `production.yaml`:

```yaml
watchlist:
  default_alert_rules:
    - name: "absolute_size"
      expression: "trade.size >= 1000"
      severity: "critical"
    - name: "p95_delta_weighted"
      expression: "trade.is_option && trade.delta_weighted_size >= config.p95_delta_weighted_size"
      severity: "high"
    - name: "toxic_flow"
      expression: "config.vpin >= config.vpin_threshold && trade.delta_weighted_size > config.p95_delta_weighted_size"
      severity: "high"

  tickers:
    - symbol: "SPY"
      option_type: "equity"
      expiration_filter: "0DTE"
      underlying_alert_rules:
        - name: "huge_option_print"
          expression: "trade.is_option && trade.size >= 90 && trade.price > .25"
          severity: "high"

    - symbol: "/ES"
      option_type: "futures"
      expiration_filter: "0DTE"
      underlying_alert_rules:
        - name: "large_option_print"
          expression: "trade.is_option && trade.size >= 5 && trade.price > 2.0"
          severity: "medium"

detection:
  size_mult: 2.0
  abs_min_size: 5
  fdr_alpha: 0.05
  vpin_threshold: 0.6
  p95_by_regime:
    low_vol:
      p95_size: 80
      p95_delta_weighted_size: 100000
    normal:
      p95_size: 120
      p95_delta_weighted_size: 200000
    high_vol:
      p95_size: 200
      p95_delta_weighted_size: 500000
    crash:
      p95_size: 500
      p95_delta_weighted_size: 1000000
  dynamic_thresholds:
    p95_size:
      expression: "bayesian_mean * 10"
      regime_adjustment:
        low_vol: 0.8
        normal: 1.0
        high_vol: 1.5
        crash: 2.0
      vol_target: true

dxlink:
  max_payload_bytes: 60000
  chunk_delay_sec: 0.1
  enable_chunking: true

outputs:
  stdout: true
  persist_events: true
  data_dir: "data/events"
```

See [docs/configuration.md](docs/configuration.md) for the full reference.

## Run

```bash
uv run python -m dxlink_scanner.cli --config production.yaml --verbose
# or
uv run dxlink-scanner --config production.yaml --verbose --debug-messages
```

## Test

```bash
uv run pytest tests/ -v
```

## Lint / Typecheck

```bash
uv run ruff check .
uv run mypy .
```

## Documentation

| Topic | Document |
|-------|----------|
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Configuration Reference | [docs/configuration.md](docs/configuration.md) |
| CEL Rule Engine | [docs/cel_rules.md](docs/cel_rules.md) |
| Statistical Models | [docs/model_cards.md](docs/model_cards.md) |
| DXLink Chunking Design | [docs/dxlink_chunking_design.md](docs/dxlink_chunking_design.md) |
| Filtering Research | [docs/filtering.md](docs/filtering.md) |

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

## Stats Pipeline

The scanner maintains per-symbol statistical models updated on each event:

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