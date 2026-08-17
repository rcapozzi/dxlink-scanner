"""CEL-based rule engine for per-symbol alert evaluation.

Uses the Common Expression Language (CEL) to evaluate configurable
alert rules. Rules are defined as YAML strings and compiled once at
startup, then evaluated in microseconds per event.

Example CEL rules:
    trade.size >= 100                          # absolute threshold
    trade.size >= 50 && option.type == "call"  # option-specific
    trade.size > stats.median * 5.0            # anomaly detection
    trade.size >= 100 && is_rth(trade.timestamp)  # time-gated
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from cel_expr_python import cel

from dxlink_scanner.config import CelAlertRule, DetectionConfig, WatchlistConfig
from dxlink_scanner.models import Alert, RollingStats, TimeAndSaleEvent, _to_epoch_ms
from dxlink_scanner.snapshot_store import SnapshotStore
from dxlink_scanner.stats import (
    BayesianGammaPoisson,
    CrossAssetHawkes,
    CrossSymbolPool,
    FlowMetrics,
    HawkesProcess,
    RegimeDetector,
    RollingStatsManager,
    RollingStatsV2,
    TimeOfDaySeasonality,
    VolatilityTargeter,
    VolumeAtPrice,
    bayesian_decision,
    online_fdr_threshold,
)

if TYPE_CHECKING:
    from dxlink_scanner.stats import RollingStatsManagerV2

logger = logging.getLogger(__name__)


def _compute_mean(rolling: RollingStats | RollingStatsV2) -> float:
    """Compute the arithmetic mean of a RollingStats deque."""
    if isinstance(rolling, RollingStatsV2):
        return float(rolling.mean())
    sizes = getattr(rolling, "sizes", None)
    if not sizes:
        return 0.0
    return float(sum(sizes) / len(sizes))


class CELRuleEngine:
    """CEL-based rule engine for per-symbol alert evaluation.

    Rules are compiled once at construction and cached for reuse.
    Supports three tiers of rule resolution, evaluated in order:

    1. **Per-symbol rules** — exact match on the streamer symbol
       (including individual option symbols).
    2. **Underlying-scoped rules** — resolved from the
       ``underlying_alert_rules`` list on the matching ``TickerConfig``.
       When an option trade event arrives, its underlying symbol is
       resolved via ``underlying_symbol_map`` and the corresponding rules
       are evaluated. This lets a single rule definition cover **all
       options of one underlying** (e.g. every SPY call/put).
    3. **Default rules** — global fallback for any symbol without a
       per-symbol or underlying match.

    Attributes:
        config: Detection thresholds (abs_min_size, size_mult retained for compat).
        stats: Rolling statistics manager for stats.* variables in rules.
        _compiled_rules: Dict mapping (symbol, rule_name) -> compiled CEL expression.
        _per_symbol_rules: Dict mapping symbol -> list[CelAlertRule].
        _default_rules: List of global default rules.
    """

    def __init__(
        self,
        config: DetectionConfig,
        watchlist: WatchlistConfig,
        stats: RollingStatsManager | RollingStatsManagerV2,
        per_symbol_rules: dict[str, list[CelAlertRule]] | None = None,
        default_rules: list[CelAlertRule] | None = None,
        underlying_symbols: set[str] | None = None,
        underlying_symbol_map: dict[str, str] | None = None,
        snapshot_store: SnapshotStore | None = None,
        significance_thresholds: dict[str, dict[str, float]] | None = None,
        # Statistical models for enhanced analysis
        bayesian_models: dict[str, BayesianGammaPoisson] | None = None,
        hawkes_models: dict[str, HawkesProcess] | None = None,
        seasonality_models: dict[str, TimeOfDaySeasonality] | None = None,
        cross_symbol_pool: CrossSymbolPool | None = None,
        regime_detectors: dict[str, RegimeDetector] | None = None,
        volume_at_price_models: dict[str, VolumeAtPrice] | None = None,
        flow_metrics: dict[str, FlowMetrics] | None = None,
        cross_asset_hawkes: CrossAssetHawkes | None = None,
    ) -> None:
        self._config = config
        self._stats = stats
        self._watchlist = watchlist
        self._underlying_symbols = underlying_symbols or set()
        self._underlying_symbol_map: dict[str, str] = underlying_symbol_map or {}
        self._snapshot_store = snapshot_store
        self._significance_thresholds: dict[str, dict[str, float]] = significance_thresholds or {}

        # Statistical models
        self._bayesian_models: dict[str, BayesianGammaPoisson] = bayesian_models or {}
        self._hawkes_models: dict[str, HawkesProcess] = hawkes_models or {}
        self._seasonality_models: dict[str, TimeOfDaySeasonality] = seasonality_models or {}
        self._cross_symbol_pool: CrossSymbolPool | None = cross_symbol_pool
        self._regime_detectors: dict[str, RegimeDetector] = regime_detectors or {}
        self._last_config: dict[str, Any] = {}
        self._vap_models: dict[str, VolumeAtPrice] = volume_at_price_models or {}
        self._flow_metrics: dict[str, FlowMetrics] = flow_metrics or {}
        self._cross_asset_hawkes: CrossAssetHawkes | None = cross_asset_hawkes

        # Per-symbol rules: symbol -> list of (rule, compiled_expr)
        self._per_symbol_rules: dict[str, list[CelAlertRule]] = per_symbol_rules or {}
        self._default_rules: list[CelAlertRule] = default_rules or []

        # Underlying-scoped rules: underlying_symbol -> list of (rule, compiled_expr)
        # Collected from each TickerConfig's underlying_alert_rules field.
        self._underlying_rules: dict[str, list[CelAlertRule]] = self._collect_underlying_rules()

        # Compile all expressions once at startup
        self._compiled: dict[str, list[tuple[CelAlertRule, cel.Expression]]] = {}
        self._compile_all()

        # Dynamic threshold manager
        self._dtm: DynamicThresholdManager | None = None
        if config.dynamic_thresholds is not None:
            from dxlink_scanner.stats.dynamic_thresholds import DynamicThresholdManager

            self._dtm = DynamicThresholdManager(config)
            self._dtm.register_dynamic_thresholds(config.dynamic_thresholds)

    def _cel_env(self) -> cel.Env:
        """Create a CEL environment with variable declarations and custom functions."""
        # Decision-theoretic CEL functions
        fdr_alpha = self._config.fdr_alpha
        fdr_lag = self._config.fdr_lag

        # Online FDR state (mutable closure)
        _fdr_state = {"num_rejections": 0, "num_tests": 0}

        def _bayesian_decision(cost_f_p: float, cost_f_n: float) -> bool:
            """Bayesian decision rule using the current symbol's model."""
            # This is a macro-like function: the caller should use the
            # pre-computed bayesian_decision result in config_data.
            # Actual implementation in _build_activation stores the result.
            return True

        def _online_fdr_pvalue(pval: float) -> bool:
            """Online FDR: reject if p-value < adjusted threshold."""
            _fdr_state["num_tests"] += 1
            threshold = online_fdr_threshold(
                fdr_alpha,
                _fdr_state["num_rejections"],
                _fdr_state["num_tests"],
                lag=fdr_lag,
            )
            if pval <= threshold:
                _fdr_state["num_rejections"] += 1
                return True
            return False

        def _hierarchical_fdr(pval: float) -> bool:
            """Simplified hierarchical FDR (per-symbol level)."""
            return _online_fdr_pvalue(pval)

        # Register function declarations with implementations
        bayesian_decision_decl = cel.FunctionDecl(
            name="bayesian_decision",
            overloads=[
                cel.Overload(
                    id="bayesian_decision_2",
                    return_type=cel.Type.BOOL,
                    parameters=[cel.Type.DOUBLE, cel.Type.DOUBLE],
                    impl=_bayesian_decision,
                )
            ],
        )
        online_fdr_decl = cel.FunctionDecl(
            name="online_fdr_pvalue",
            overloads=[
                cel.Overload(
                    id="online_fdr_pvalue_1",
                    return_type=cel.Type.BOOL,
                    parameters=[cel.Type.DOUBLE],
                    impl=_online_fdr_pvalue,
                )
            ],
        )
        hierarchical_fdr_decl = cel.FunctionDecl(
            name="hierarchical_fdr",
            overloads=[
                cel.Overload(
                    id="hierarchical_fdr_1",
                    return_type=cel.Type.BOOL,
                    parameters=[cel.Type.DOUBLE],
                    impl=_hierarchical_fdr,
                )
            ],
        )

        return cel.NewEnv(
            variables={
                # Core trade fields
                "trade": cel.Type.Map(cel.Type.STRING, cel.Type.DYN),
                # Option metadata (strike, type)
                "option": cel.Type.Map(cel.Type.STRING, cel.Type.DYN),
                # Underlying info
                "underlying": cel.Type.Map(cel.Type.STRING, cel.Type.DYN),
                # Rolling stats (pre-computed values)
                "stats": cel.Type.Map(cel.Type.STRING, cel.Type.DYN),
                # Config thresholds
                "config": cel.Type.Map(cel.Type.STRING, cel.Type.DYN),
            },
            functions=[bayesian_decision_decl, online_fdr_decl, hierarchical_fdr_decl],
        )

    def _collect_underlying_rules(self) -> dict[str, list[CelAlertRule]]:
        """Collect underlying_alert_rules from each TickerConfig, keyed by underlying symbol."""
        rules_map: dict[str, list[CelAlertRule]] = {}
        for ticker in self._watchlist.tickers:
            if ticker.underlying_alert_rules:
                rules_map[ticker.symbol] = ticker.underlying_alert_rules
        return rules_map

    def _compile_all(self) -> None:
        """Compile all CEL rule expressions once and cache them."""
        env = self._cel_env()
        for symbol, rules in self._per_symbol_rules.items():
            self._compiled[symbol] = []
            for rule in rules:
                try:
                    expr = env.compile(rule.expression, True)
                    self._compiled[symbol].append((rule, expr))
                except Exception as e:
                    logger.error(
                        "Failed to compile rule '%s' for symbol '%s': %s",
                        rule.name,
                        symbol,
                        e,
                    )
        # Compile underlying-scoped rules under their underlying symbol key
        for underlying, rules in self._underlying_rules.items():
            self._compiled[underlying] = []
            for rule in rules:
                try:
                    expr = env.compile(rule.expression, True)
                    self._compiled[underlying].append((rule, expr))
                except Exception as e:
                    logger.error(
                        "Failed to compile underlying-scoped rule '%s' for '%s': %s",
                        rule.name,
                        underlying,
                        e,
                    )
        # Compile default rules under the special "__default__" key
        self._compiled["__default__"] = []
        for rule in self._default_rules:
            try:
                expr = env.compile(rule.expression, True)
                self._compiled["__default__"].append((rule, expr))
            except Exception as e:
                logger.error(
                    "Failed to compile default rule '%s': %s",
                    rule.name,
                    e,
                )

    def _build_activation(
        self,
        event: TimeAndSaleEvent,
        symbol: str,
    ) -> dict[str, Any]:
        """Build the CEL activation (variable bindings) for a given event.

        Converts Decimal → float for CEL compatibility, since CEL
        doesn't natively support Python Decimal types.
        """
        is_option = symbol not in self._underlying_symbols

        # Core trade fields — convert Decimal to float for CEL compatibility
        delta_float = float(event.delta) if event.delta else 0.0
        delta_weighted_size = int(float(event.size) * abs(delta_float)) if delta_float else event.size
        trade_data: dict[str, Any] = {
            "symbol": symbol,
            "is_option": is_option,
            "price": float(event.price),
            "size": event.size,
            # CEL doesn't natively support datetime; convert to ISO string
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "delta": delta_float,
            "delta_weighted_size": delta_weighted_size,
            "bid_price": float(event.bid_price) if event.bid_price else None,
            "ask_price": float(event.ask_price) if event.ask_price else None,
            "trade_type": event.trade_type,
        }

        # Rolling stats
        rolling = self._stats.get(event.symbol)
        # Check if it's V2 stats for enhanced features
        if rolling is not None and isinstance(rolling, RollingStatsV2):
            rolling_v2 = rolling
            stats_data: dict[str, Any] = {
                "median": rolling_v2.median(),
                "mad": rolling_v2.mad(),
                "count": rolling_v2.count,
                "mean": rolling_v2.mean(),
                "std": rolling_v2.std(),
                "p25": rolling_v2.percentile(25),
                "p75": rolling_v2.percentile(75),
                "p90": rolling_v2.percentile(90),
                "p95": rolling_v2.percentile(95),
                "p99": rolling_v2.percentile(99),
                "z_score": rolling_v2.z_score(float(event.size)),
                "modified_z_score": rolling_v2.modified_z_score(float(event.size)),
            }
            # Add session-aware stats if available
            if len(rolling_v2._rth_window) > 0:
                stats_data["rth_median"] = rolling_v2.rth_median()
                stats_data["rth_mean"] = rolling_v2.rth_mean()
                stats_data["rth_std"] = rolling_v2.rth_std()
            if len(rolling_v2._eth_window) > 0:
                stats_data["eth_median"] = rolling_v2.eth_median()
                stats_data["eth_mean"] = rolling_v2.eth_mean()
                stats_data["eth_std"] = rolling_v2.eth_std()
        else:
            stats_data = {
                "median": rolling.median() if rolling else 0.0,
                "mad": rolling.mad() if rolling else 0.0,
                "count": len(getattr(rolling, "sizes", [])) if rolling else 0,
                "mean": _compute_mean(rolling) if rolling else 0.0,
            }

        # Config thresholds
        config_data: dict[str, Any] = {
            "abs_min_size": self._config.abs_min_size,
            "size_mult": self._config.size_mult,
            "vpin_threshold": self._config.vpin_threshold,
            "vol_target": self._config.vol_target,
        }

        # Significance thresholds from daily P95 analysis
        thresholds = self._significance_thresholds.get(symbol) or self._significance_thresholds.get("default")
        if thresholds:
            config_data["p95_size"] = thresholds.get("p95_size", 0.0)
            config_data["p95_delta_weighted_size"] = thresholds.get("p95_delta_weighted_size", 0.0)

        # Statistical model outputs
        # Bayesian Gamma-Poisson
        bayesian = self._bayesian_models.get(symbol) or self._bayesian_models.get("default")
        if bayesian:
            config_data["bayesian_mean"] = bayesian.posterior_mean()
            config_data["bayesian_alpha"] = bayesian.alpha_post
            config_data["bayesian_beta"] = bayesian.beta_post
            ci = bayesian.credible_interval(0.95)
            config_data["bayesian_ci_low"] = ci[0]
            config_data["bayesian_ci_high"] = ci[1]

            # Decision-theoretic: p-value and Bayes factor for this trade
            from dxlink_scanner.stats import bayesian_anomaly_score

            anomaly_metrics = bayesian_anomaly_score(event.size, bayesian, exposure=1.0)
            config_data["bayesian_p_value"] = anomaly_metrics["p_value"]
            config_data["bayes_factor"] = anomaly_metrics["bayes_factor"]
            config_data["bayesian_decision"] = bayesian_decision(
                bayesian,
                event.size,
                cost_fp=self._config.cost_false_positive,
                cost_fn=self._config.cost_false_negative,
            )
            config_data["fdr_alpha"] = self._config.fdr_alpha

        # Hawkes process
        hawkes = self._hawkes_models.get(symbol) or self._hawkes_models.get("default")
        if hawkes:
            current_time = dt.datetime.now(dt.UTC).timestamp()
            config_data["hawkes_intensity"] = hawkes.current_intensity(current_time)
            config_data["hawkes_expected_60s"] = hawkes.expected_events(60.0, current_time)
            config_data["hawkes_mu"] = hawkes.mu
            config_data["hawkes_alpha"] = hawkes.alpha
            config_data["hawkes_beta"] = hawkes.beta

        # Time-of-day seasonality
        seasonality = self._seasonality_models.get(symbol) or self._seasonality_models.get("default")
        if seasonality and event.timestamp:
            factor = seasonality.seasonality_factor(event.timestamp)
            expected = seasonality.expected_volume(event.timestamp)
            config_data["seasonality_factor"] = factor
            config_data["seasonality_expected_volume"] = expected
            # Seasonally adjusted size
            config_data["seasonal_adj_size"] = int(event.size / factor) if factor > 0 else event.size

        # Regime detection
        regime_detector = self._regime_detectors.get(symbol) or self._regime_detectors.get("default")
        vol_targeter = VolatilityTargeter(vol_target=self._config.vol_target)
        if regime_detector:
            regime_state = regime_detector.detect()
            config_data["vol_ratio"] = (
                regime_state.volatility / self._config.vol_target if self._config.vol_target > 0 else 1.0
            )
            config_data["vol_targeted_threshold"] = vol_targeter.adjusted_threshold(
                regime_state.volatility,
                config_data.get("p95_size", 0.0),
            )
            config_data["regime"] = regime_state.regime
            config_data["regime_prob"] = regime_state.probability
            config_data["regime_volatility"] = regime_state.volatility
            config_data["regime_volume_rate"] = regime_state.volume_rate
            # Regime-conditioned P95 thresholds (if configured)
            if self._config.p95_by_regime:
                regime_name = {0: "low_vol", 1: "normal", 2: "high_vol", 3: "crash"}.get(regime_state.regime, "normal")
                regime_thresholds = self._config.p95_by_regime.get(regime_name, {})
                if regime_thresholds:
                    config_data["p95_size"] = regime_thresholds.get("p95_size", config_data.get("p95_size", 0.0))
                    config_data["p95_delta_weighted_size"] = regime_thresholds.get(
                        "p95_delta_weighted_size", config_data.get("p95_delta_weighted_size", 0.0)
                    )

        # Determine if this is an option symbol or an underlying
        option_data: dict[str, Any] | None = None
        underlying_data: dict[str, Any] | None = None

        if is_option:
            option_data = {
                "type": "call",  # TODO: derive from symbol suffix
                "strike": 0.0,  # TODO: parse from streamer symbol
            }
        else:
            underlying_data = {
                "symbol": symbol,
            }

        # Cross-symbol pooled estimates (for sparse data)
        if self._cross_symbol_pool:
            pooled_mean = self._cross_symbol_pool.get_pooled_estimate(symbol)
            config_data["bayesian_pooled_mean"] = pooled_mean
            ci_low, ci_high = self._cross_symbol_pool.credible_interval(symbol, 0.95)
            config_data["bayesian_pooled_ci_low"] = ci_low
            config_data["bayesian_pooled_ci_high"] = ci_high

        # Volume at Price (VAP) profile
        vap = self._vap_models.get(symbol) or self._vap_models.get("default")
        if vap:
            config_data["vap_poc"] = vap.poc()
            va_low, va_high = vap.value_area(0.70)
            config_data["vap_val_area_low"] = va_low
            config_data["vap_val_area_high"] = va_high
            config_data["vap_imbalance"] = vap.imbalance()

        # Flow metrics (VPIN, trade classification, liquidity)
        flow = self._flow_metrics.get(symbol) or self._flow_metrics.get("default")
        if flow:
            config_data["vpin"] = flow.vpin.vpin
            config_data["vpin_std"] = flow.vpin.vpin_std
            config_data["spread_p50"] = flow.liquidity.spread_p50
            config_data["spread_p95"] = flow.liquidity.spread_p95
            config_data["depth_at_poc_median"] = flow.liquidity.depth_at_poc_median
            # Classify the trade direction
            snap = self._snapshot_store.get(event.symbol) if self._snapshot_store else None
            bid_price = snap.bid_price if snap else None
            ask_price = snap.ask_price if snap else None
            mid_price = snap.mid_price if snap else None
            prev_mid = flow.prev_mid_price
            tick_dir = None
            if prev_mid is not None:
                tick_dir = 1 if float(event.price) > prev_mid else (-1 if float(event.price) < prev_mid else 0)
            classification = flow.trade_classifier.classify(
                float(event.price) if event.price else 0.0,
                float(bid_price) if bid_price else None,
                float(ask_price) if ask_price else None,
                float(mid_price) if mid_price else prev_mid,
                tick_dir,
            )
            config_data["trade_side"] = classification.side
            config_data["trade_classification_confidence"] = classification.confidence

        # Cross-asset Hawkes (systemic flow)
        if self._cross_asset_hawkes:
            config_data["systemic_score"] = self._cross_asset_hawkes.systemic_anomaly_score()
            config_data["cross_asset_vpin"] = flow.vpin.vpin if flow else 0.5

        # Dynamic threshold overrides (expression-based, computed from stats)
        dynamic = self._compute_dynamic_thresholds(symbol, config_data)
        config_data.update(dynamic)

        activation: dict[str, Any] = {
            "trade": trade_data,
            "stats": stats_data,
            "config": config_data,
        }
        if option_data:
            activation["option"] = option_data
        if underlying_data:
            activation["underlying"] = underlying_data

        # Store last config for alert enrichment
        self._last_config = config_data

        return activation

    def _compute_dynamic_thresholds(self, symbol: str, config_data: dict[str, Any]) -> dict[str, float]:
        """Compute expression-based dynamic thresholds from current stats context.

        Uses DynamicThresholdManager to evaluate threshold expressions
        referencing statistical model outputs (e.g. 'bayesian_mean * 5').
        Returns empty dict if no dynamic thresholds are configured.
        """
        if self._dtm is None:
            return {}

        # Build regime/volatility context from config_data
        regime = {0: "low_vol", 1: "normal", 2: "high_vol", 3: "crash"}.get(config_data.get("regime", 1), "normal")

        vol_ratio = config_data.get("vol_ratio")
        volatility = config_data.get("regime_volatility")

        # Build stats context from config_data (which already has model outputs)
        stats_context = {
            k: float(v) for k, v in config_data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
        }

        return self._dtm.compute_thresholds(
            symbol,
            rolling_stats=stats_context,
            regime=regime,
            vol_ratio=vol_ratio,
            volatility=volatility,
        )

    def _resolve_underlying(self, symbol: str) -> str | None:
        """Resolve an option streamer symbol to its underlying symbol.

        Uses the underlying_symbol_map (option_symbol -> underlying_symbol)
        passed in at construction. Falls back to prefix matching against
        the watchlist's known underlying symbols.

        Args:
            symbol: A streamer symbol (e.g. an option like ".SPY260731C500").

        Returns:
            The underlying symbol (e.g. "SPY" or "/ES") or None if not resolvable.
        """
        underlying = self._underlying_symbol_map.get(symbol)
        if underlying:
            # For futures, the map value may be the streamer-root-symbol
            # (e.g. /ES:XCME) but underlying-scoped rules are compiled
            # under ticker.symbol (e.g. /ES). Try ticker.symbol as fallback.
            if underlying in self._compiled:
                return underlying
            for ticker in self._watchlist.tickers:
                if ticker.symbol in self._compiled:
                    return ticker.symbol
            return underlying
        # Fallback: prefix-match against known underlying symbols in the watchlist
        for ticker in self._watchlist.tickers:
            if ticker.symbol in self._underlying_symbols and symbol.startswith(ticker.symbol):
                return ticker.symbol
        return None

    def process(self, event: TimeAndSaleEvent) -> Alert | None:
        """Process a trade event and return an Alert if any rule matches.

        Evaluates rules in three tiers, first match wins:
            1. Per-symbol rules — exact match on event.symbol
            2. Underlying-scoped rules — resolved from the underlying
               of this symbol (via underlying_symbol_map). This allows
               a single rule to cover all options of one underlying.
            3. Default rules — global fallback

        Args:
            event: A TimeAndSaleEvent from the feed.

        Returns:
            An Alert if any rule matches, None otherwise.
        """
        symbol = event.symbol
        activation = self._build_activation(event, symbol)

        # Build the ordered list of rule tiers to evaluate.
        # Tier 1: per-symbol rules (exact symbol match)
        # Tier 2: underlying-scoped rules (resolved from the underlying of an option symbol)
        # Tier 3: default rules (global fallback)
        rule_sets: list[list[tuple[CelAlertRule, cel.Expression]]] = []

        # Tier 1
        rule_sets.append(self._compiled.get(symbol, []))

        # Tier 2 — only for option symbols (not underlyings themselves)
        if symbol not in self._underlying_symbols:
            underlying = self._resolve_underlying(symbol)
            if underlying:
                rule_sets.append(self._compiled.get(underlying, []))

        # Tier 3
        rule_sets.append(self._compiled.get("__default__", []))

        for rules in rule_sets:
            for rule, expr in rules:
                try:
                    result = expr.eval(None, data=activation)
                    if result.plain_value() is True:
                        # Look up the most recent underlying_price from the snapshot store.
                        # underlying_price is derived from Quote mid_price on the
                        # underlying symbol (e.g. /ES:XCME for futures, SPY for equities).
                        underlying_price: float | None = None
                        if self._snapshot_store is not None:
                            snap = self._snapshot_store.get(event.symbol)
                            if snap is not None and snap.underlying_symbol:
                                under_snap = self._snapshot_store.get(snap.underlying_symbol)
                                if under_snap is not None and under_snap.mid_price is not None:
                                    underlying_price = float(under_snap.mid_price)
                        return Alert(
                            symbol=event.symbol,
                            price=event.price,
                            size=event.size,
                            timestamp_ms=_to_epoch_ms(event.timestamp) or 0,
                            rule_name=rule.name,
                            severity=rule.severity,
                            underlying_price=underlying_price,
                            posterior_mean=self._last_config.get("bayesian_mean"),
                            bayes_factor=self._last_config.get("bayes_factor"),
                            p_value=self._last_config.get("bayesian_p_value"),
                            decision_threshold=self._last_config.get("bayesian_decision"),
                            alert_utility=self._last_config.get("bayes_factor"),
                            is_regime_shift=False,  # Set True when regime transition rules match
                            vpin=self._last_config.get("vpin"),
                            trade_side=self._last_config.get("trade_side"),
                        )
                except Exception as e:
                    logger.warning(
                        "CEL evaluation error in rule '%s' for %s: %s",
                        rule.name,
                        symbol,
                        e,
                    )

        return None

    async def aprocess(self, event: TimeAndSaleEvent) -> Alert | None:
        """Async-compatible process (delegates to sync)."""
        return self.process(event)

    @property
    def rule_count(self) -> int:
        """Total number of compiled rules (per-symbol + underlying + defaults)."""
        total = sum(len(rules) for rules in self._compiled.values())
        return total
