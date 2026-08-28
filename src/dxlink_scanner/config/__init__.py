"""Configuration schema and loader for the scanner."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class CelAlertRule(BaseModel):
    """A CEL-based alert rule defined in config.

    Attributes:
        name: Human-readable rule name.
        expression: CEL expression string (e.g. "trade.size >= 100").
        severity: Alert severity ("info", "low", "medium", "high", "critical").
    """

    name: str
    expression: str
    severity: str = Field(default="high", pattern=r"^(info|low|medium|high|critical)$")


class TastyTradeConfig(BaseModel):
    """Tastytrade API credentials."""

    client_id: str
    client_secret: str
    refresh_token: str
    sandbox: bool = False


class TickerConfig(BaseModel):
    """Per-ticker watchlist configuration.

    Attributes:
        symbol: Underlying symbol (e.g. "SPY").
        option_type: "equity" for stock/ETF options, "futures" for index/futures options (e.g. SPX).
        expiration_filter: "0DTE" for same-day expiry only, "all" for all expirations.
        alert_rules: List of CEL-based alert rules for this symbol.
            These match the exact streamer symbol (including option symbols).
        underlying_alert_rules: List of CEL-based alert rules that apply to
            ALL option symbols of this underlying. When an option trade event
            is processed, its underlying symbol is resolved (via the
            underlying_symbol_map) and these rules are evaluated against it.
            This avoids having to enumerate every option symbol individually.
    """

    symbol: str
    option_type: str = Field(default="equity", pattern=r"^(equity|futures)$")
    expiration_filter: str = Field(default="0DTE", pattern=r"^(0DTE|all)$")
    alert_rules: list[CelAlertRule] = Field(default_factory=list)
    underlying_alert_rules: list[CelAlertRule] = Field(default_factory=list)


class WatchlistConfig(BaseModel):
    """Watchlist configuration for option chain selection.

    Attributes:
        tickers: List of per-ticker configurations.
        default_alert_rules: Global fallback CEL rules applied when no
            per-symbol or underlying-scoped rule matches.
    """

    tickers: list[TickerConfig] = Field(default=[TickerConfig(symbol="SPY")])
    default_alert_rules: list[CelAlertRule] = Field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        """Return the list of underlying symbols."""
        return [t.symbol for t in self.tickers]


class DetectionConfig(BaseModel):
    """Anomaly detection thresholds."""

    size_mult: float = Field(default=5.0, gt=0)
    abs_min_size: int = Field(default=10, ge=1)
    stats_window: int = Field(default=50, ge=10, le=500)
    # V2 options
    stats_half_life_sec: float | None = Field(default=None, gt=0)
    stats_session_aware: bool = False
    # Decision-theoretic alerting
    cost_false_positive: float = Field(default=1.0, gt=0)
    cost_false_negative: float = Field(default=10.0, gt=0)
    cost_missed_regime_shift: float = Field(default=50.0, gt=0)
    # Online FDR
    fdr_alpha: float = Field(default=0.05, gt=0, le=1)
    fdr_lag: int = Field(default=0, ge=0)
    # Regime-aware thresholds
    vol_low: float = Field(default=0.01, gt=0)
    vol_high: float = Field(default=0.03, gt=0)
    vol_crash: float = Field(default=0.05, gt=0)
    vol_target: float = Field(default=0.02, gt=0)
    # Microstructure toxicity thresholds
    vpin_threshold: float = Field(default=0.6, gt=0, le=1)
    vpin_window_buckets: int = Field(default=265, ge=1)
    vpin_bucket_volume: int = Field(default=1000, ge=1)
    # Regime-conditioned P95 thresholds (overrides base significance thresholds)
    p95_by_regime: dict[str, dict[str, float]] | None = None
    # Expression-based dynamic thresholds referencing statistical model outputs
    dynamic_thresholds: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description="Expression-based threshold definitions referencing model stats. "
        "Keys are threshold names, values are dicts with 'expression' (e.g. 'bayesian_mean * 5'), "
        "'regime_adjustment' (multipliers per regime), 'vol_target' (bool to apply vol targeting).",
    )


class WebhookConfig(BaseModel):
    """Webhook output configuration."""

    enabled: bool = False
    url: str | None = None
    timeout_seconds: float = Field(default=5.0, gt=0)
    max_retries: int = Field(default=3, ge=0)


class OutputsConfig(BaseModel):
    """Alert output sinks."""

    stdout: bool = True
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    data_dir: str = Field(
        default="data/events",
        description="Directory for parquet event storage",
    )
    persist_events: bool = Field(
        default=False,
        description="Whether to persist events to parquet (also settable via --persist CLI flag)",
    )
    significance_thresholds_file: str | None = Field(
        default=None,
        description="Path to significance_thresholds.json (from compact_parquet.py).",
    )
    models_state_file: str | None = Field(
        default=None,
        description="Path to models_meta.json for model state persistence (read/written by ModelStore).",
    )


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    json_format: bool = True


class StreamConfig(BaseModel):
    """Streaming configuration for event consolidation."""

    backpressure_queue_size: int = Field(default=500, gt=0, le=100000)
    flush_interval_sec: float = Field(default=5.0, gt=0)
    flush_batch_size: int = Field(default=10000, gt=0, le=1000000)
    tick_size: float = Field(default=0.01, gt=0)


class DXLinkConfig(BaseModel):
    """DXLink WebSocket configuration for payload chunking."""

    max_payload_bytes: int = Field(
        default=60_000,
        ge=10_000,
        le=64_000,
        description="Maximum bytes per FEED_SUBSCRIPTION message (DXLink limit is 64k).",
    )
    chunk_delay_sec: float = Field(
        default=0.1,
        ge=0,
        le=5.0,
        description="Delay between sending chunks to avoid rate limiting.",
    )
    enable_chunking: bool = Field(
        default=True,
        description="Enable automatic payload chunking for subscriptions.",
    )


class ScannerConfig(BaseModel):
    """Top-level scanner configuration."""

    tastytrade: TastyTradeConfig
    watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    dxlink: DXLinkConfig = Field(default_factory=DXLinkConfig)


def _resolve_env(value: str) -> str:
    """Resolve ${VAR} patterns in a string using environment variables."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        return os.environ.get(var_name, "")
    return value


def _resolve_env_recursive(obj: object) -> object:
    """Recursively resolve ${VAR} patterns in a dict/list structure."""
    if isinstance(obj, dict):
        return {k: _resolve_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_recursive(v) for v in obj]
    if isinstance(obj, str):
        return _resolve_env(obj)
    return obj


def load_config(config_path: str | Path) -> ScannerConfig:
    """Load scanner configuration from a YAML file.

    Resolves ${VAR} patterns from environment variables.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        A validated ScannerConfig instance.

    Raises:
        FileNotFoundError: If the config file doesn't exist.
        ValidationError: If the config doesn't match the schema.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)
    resolved = _resolve_env_recursive(raw)
    return ScannerConfig.model_validate(resolved)
