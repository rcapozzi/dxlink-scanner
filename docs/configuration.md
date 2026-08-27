# Configuration Reference

Complete reference for the scanner YAML configuration file with environment variable substitution.

## Configuration File Structure

```yaml
# Top-level sections
tastytrade:          # Authentication credentials (required)
watchlist:           # Symbols and per-ticker settings (required)
detection:           # Anomaly detection thresholds (optional, defaults shown)
outputs:             # Alert output sinks (optional, defaults shown)
logging:             # Logging configuration (optional, defaults shown)
stream:              # Streaming/backpressure settings (optional, defaults shown)
```

## Environment Variable Substitution

The config loader resolves `${VAR}` patterns using environment variables:

```yaml
tastytrade:
  client_id: ${TASTY_CLIENT_ID}
  client_secret: ${TASTY_CLIENT_SECRET}
  refresh_token: ${TASTY_REFRESH_TOKEN}
```

**Supported patterns**: `${VAR_NAME}` only (no default values, no nested substitution).

**Required environment variables** (set in shell or `.env`):
- `TASTY_CLIENT_ID` — Tastytrade API client ID
- `TASTY_CLIENT_SECRET` — Tastytrade API client secret
- `TASTY_REFRESH_TOKEN` — Long-lived refresh token
- `TASTY_SANDBOX` — `"true"` or `"false"` (optional, defaults to false)

## Complete Configuration Schema

### `tastytrade` — Authentication (Required)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `client_id` | string | ✅ | — | Tastytrade OAuth2 client ID |
| `client_secret` | string | ✅ | — | Tastytrade OAuth2 client secret |
| `refresh_token` | string | ✅ | — | Long-lived refresh token |
| `sandbox` | boolean | ❌ | `false` | Use sandbox environment |

```yaml
tastytrade:
  client_id: ${TASTY_CLIENT_ID}
  client_secret: ${TASTY_CLIENT_SECRET}
  refresh_token: ${TASTY_REFRESH_TOKEN}
  sandbox: false
```

---

### `watchlist` — Symbols & Per-Ticker Config (Required)

```yaml
watchlist:
  default_alert_rules: []  # Global fallback CEL rules (optional)
  tickers:
    - symbol: "SPY"
      option_type: "equity"
      expiration_filter: "0DTE"
      alert_rules: []  # CEL rules (optional)
      underlying_alert_rules: []  # CEL rules for all options of this underlying (optional)
```

#### Per-Ticker Fields (`TickerConfig`)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `symbol` | string | ✅ | — | Underlying symbol (SPY, QQQ, SPX, ES, etc.) |
| `option_type` | string | ❌ | `"equity"` | `"equity"` for stock/ETF options, `"futures"` for index/futures options |
| `expiration_filter` | string | ❌ | `"0DTE"` | `"0DTE"` for same-day expiry only, `"all"` for all expirations |
| `alert_rules` | list | ❌ | `[]` | CEL-based alert rules (see [CEL Rules](cel_rules.md)) |
| `underlying_alert_rules` | list | ❌ | `[]` | CEL rules that apply to ALL option symbols of this underlying |

#### Option Type Values

| Value | Use For | Symbol Format |
|-------|---------|---------------|
| `equity` | SPY, QQQ, IWM, DIA, AAPL, etc. | `SPY250731C00450000:EQ` |
| `futures` | SPX, NDX, RUT, ES, NQ, etc. | `/ES:XCME`, `SPX250731C00450000` |

---

### `detection` — Anomaly Detection Thresholds (Optional)

```yaml
detection:
  size_mult: 5.0
  abs_min_size: 10
  stats_window: 50
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `size_mult` | float | ❌ | `5.0` | Anomaly multiplier: triggers when `size >= median * size_mult` (must be > 0) |
| `abs_min_size` | integer | ❌ | `10` | Absolute minimum trade size for anomaly detection (must be ≥ 1) |
| `stats_window` | integer | ❌ | `50` | Rolling window size for median/MAD calculation (10-500) |

---

### `outputs` — Alert Output Sinks (Optional)

```yaml
outputs:
  stdout: true
  webhook:
    enabled: false
    url: "http://localhost:8080/alerts"
    timeout_seconds: 5.0
    max_retries: 3
  data_dir: "data/events"
  persist_events: false
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `stdout` | boolean | ❌ | `true` | Enable JSON lines output to stdout |
| `webhook.enabled` | boolean | ❌ | `false` | Enable HTTP webhook delivery |
| `webhook.url` | string | ❌ | `null` | Webhook endpoint URL (required if enabled) |
| `webhook.timeout_seconds` | float | ❌ | `5.0` | HTTP request timeout (must be > 0) |
| `webhook.max_retries` | integer | ❌ | `3` | Max retry attempts on failure (must be ≥ 0) |
| `data_dir` | string | ❌ | `"data/events"` | Directory for Parquet event storage |
| `persist_events` | boolean | ❌ | `false` | Persist all events to Parquet files |

**Webhook Retry Logic**: Exponential backoff (1s, 2s, 4s, ...) up to `max_retries`.

---

### `logging` — Logging Configuration (Optional)

```yaml
logging:
  level: "INFO"
  json_format: true
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `level` | string | ❌ | `"INFO"` | Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `json_format` | boolean | ❌ | `true` | Output logs as JSON lines (structured) |

---

### `stream` — Streaming/Backpressure Settings (Optional)

```yaml
stream:
  backpressure_queue_size: 500
  flush_interval_sec: 5.0
  flush_batch_size: 10000
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `backpressure_queue_size` | integer | ❌ | `500` | Max events in asyncio.Queue between producers/consumer (1-100000) |
| `flush_interval_sec` | float | ❌ | `5.0` | Parquet flush interval in seconds (must be > 0) |
| `flush_batch_size` | integer | ❌ | `10000` | Max events per Parquet flush batch (1-1000000) |

**Backpressure Behavior**:
- When queue is full, producers log a warning and drop the event
- Increase `backpressure_queue_size` if seeing drops during normal operation

---

## Example Configurations

### Minimal (Equity Options Only)

```yaml
tastytrade:
  client_id: ${TASTY_CLIENT_ID}
  client_secret: ${TASTY_CLIENT_SECRET}
  refresh_token: ${TASTY_REFRESH_TOKEN}
  sandbox: false

watchlist:
  tickers:
    - symbol: "SPY"
    - symbol: "QQQ"

detection:
  size_mult: 5.0
  abs_min_size: 10

outputs:
  stdout: true
```

### Production (Multi-Asset with Alerts)

```yaml
tastytrade:
  client_id: ${TASTY_CLIENT_ID}
  client_secret: ${TASTY_CLIENT_SECRET}
  refresh_token: ${TASTY_REFRESH_TOKEN}
  sandbox: false

watchlist:
  default_alert_rules:
    - name: "absolute_size"
      expression: "trade.size >= 1000"
      severity: "critical"
  tickers:
    - symbol: "SPY"
      option_type: "equity"
      expiration_filter: "0DTE"
      underlying_alert_rules:
        - name: "large_spy_call"
          expression: "trade.is_option && trade.size >= 100 && trade.price > 1.0 && option.type == 'call'"
          severity: "high"

    - symbol: "SPX"
      option_type: "equity"
      expiration_filter: "0DTE"
      underlying_alert_rules:
        - name: "large_spx_put"
          expression: "trade.is_option && trade.size >= 50 && option.type == 'put'"
          severity: "high"

    - symbol: "ES"
      option_type: "futures"
      expiration_filter: "0DTE"
      underlying_alert_rules:
        - name: "large_es_option"
          expression: "trade.is_option && trade.size >= 5 && trade.price > 2.0"
          severity: "high"

detection:
  size_mult: 5.0
  abs_min_size: 10
  stats_window: 50

outputs:
  stdout: true
  webhook:
    enabled: true
    url: "https://alerts.example.com/webhook"
    timeout_seconds: 10.0
    max_retries: 5
  data_dir: "/data/scanner/events"
  persist_events: true

logging:
  level: "INFO"
  json_format: true

stream:
  backpressure_queue_size: 1000
  flush_interval_sec: 5.0
  flush_batch_size: 20000
```

### Development/Debug

```yaml
tastytrade:
  client_id: ${TASTY_CLIENT_ID}
  client_secret: ${TASTY_CLIENT_SECRET}
  refresh_token: ${TASTY_REFRESH_TOKEN}
  sandbox: true

watchlist:
  tickers:
    - symbol: "SPY"
      underlying_alert_rules:
        - name: "debug_print"
          expression: "trade.size >= 10"
          severity: "info"

detection:
  size_mult: 3.0
  abs_min_size: 5

outputs:
  stdout: true
  data_dir: "./data/events"
  persist_events: true

logging:
  level: "DEBUG"
  json_format: false

stream:
  backpressure_queue_size: 100
```

---

## Config Validation

The config is validated by Pydantic on load. Common validation errors:

| Error | Cause | Fix |
|-------|-------|-----|
| `Field required` | Missing required field (e.g., `client_id`) | Add field or set env var |
| `Input should be greater than 0` | `size_mult` ≤ 0 or `abs_min_size` < 1 | Use positive values |
| `Value error, Invalid option_type` | `option_type` not `"equity"` or `"futures"` | Use valid value |
| `Environment variable not found` | `${VAR}` not set in environment | Export the variable |

---

## Loading Config in Code

```python
from dxlink_scanner.config import load_config, ScannerConfig
from pathlib import Path

# Load from file (with env var substitution)
config: ScannerConfig = load_config("production.yaml")

# Access typed config
print(config.tastytrade.client_id)
print(config.watchlist.tickers[0].symbol)
print(config.detection.size_mult)

# Get all underlying symbols
underlyings = config.watchlist.symbols  # ["SPY", "QQQ", ...]
```

---

## Config File Locations

| Environment | Typical Location |
|-------------|------------------|
| Development | `dxlink-scanner.yaml` (project root) |
| Production | `/etc/dxlink-scanner/production.yaml` or `./production.yaml` |
| Container | `/config/production.yaml` (mounted volume) |

**CLI Usage**:
```bash
uv run dxlink-scanner --config production.yaml
uv run dxlink-scanner --config /etc/dxlink-scanner/production.yaml
```