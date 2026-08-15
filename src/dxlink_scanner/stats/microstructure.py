"""Microstructure analytics: order flow classification, VPIN, liquidity metrics.

Provides real-time order flow toxicity detection and liquidity profiling
for options volume anomaly detection.

Classes:
    OrderFlowClassifier — Lee-Ready / EMO trade classification (buy/sell/neutral)
    VPINCalculator — Volume-synchronized Probability of Informed Trading (Easley et al.)
    LiquidityMetrics — Bid-ask spread, depth, persistence tracking
    FlowMetrics — Aggregates all microstructure metrics per symbol
"""

from __future__ import annotations

import collections
import math
import statistics
from dataclasses import dataclass, field

from dxlink_scanner.models import ConsolidatedSnapshot


@dataclass
class TradeClassification:
    """Result of classifying a trade as buyer-initiated or seller-initiated."""
    side: str  # "buy", "sell", "unknown"
    is_informed: bool  # Whether classified using tick rule vs. bid-ask
    confidence: float  # 0.0 to 1.0


class OrderFlowClassifier:
    """Lee-Ready / EMO algorithm for trade classification.

    Uses a hybrid approach:
    1. Tick rule: if trade price crosses bid/ask midpoint, classify direction
    2. EMO (Easy-Model): if no midpoint crossing, use price direction
    3. Fallback: "unknown" if bid/ask unavailable

    Attributes:
        tick_rule_weight: Weight applied to tick-rule classification (vs EMO).
    """

    def __init__(self, tick_rule_weight: float = 0.7) -> None:
        self.tick_rule_weight = tick_rule_weight

    def classify(
        self,
        trade_price: float,
        bid_price: float | None,
        ask_price: float | None,
        prev_mid: float | None,
        tick_direction: int | None,
    ) -> TradeClassification:
        """Classify a trade as buy-initiated, sell-initiated, or unknown.

        Args:
            trade_price: Price of the trade.
            bid_price: Current bid price (may be None).
            ask_price: Current ask price (may be None).
            prev_mid: Previous midpoint price (for EMO).
            tick_direction: +1 for up-tick, -1 for down-tick, 0/None if unavailable.

        Returns:
            TradeClassification with side, informed flag, and confidence.
        """
        # If we have bid/ask and the trade price tells us which side
        if bid_price is not None and ask_price is not None and bid_price < ask_price:
            if trade_price >= ask_price:
                return TradeClassification(side="buy", is_informed=True, confidence=0.9)
            if trade_price <= bid_price:
                return TradeClassification(side="sell", is_informed=True, confidence=0.9)
            # Trade within the spread — use tick rule or EMO
            if tick_direction is not None and tick_direction != 0:
                side = "buy" if tick_direction > 0 else "sell"
                return TradeClassification(
                    side=side, is_informed=False, confidence=0.5
                )
            # EMO: compare to previous midpoint
            if prev_mid is not None:
                side = "buy" if trade_price > prev_mid else "sell"
                return TradeClassification(
                    side=side, is_informed=False, confidence=0.3
                )
            return TradeClassification(
                side="unknown", is_informed=False, confidence=0.0
            )

        # No bid/ask available — use tick rule or EMO
        if tick_direction is not None and tick_direction != 0:
            side = "buy" if tick_direction > 0 else "sell"
            return TradeClassification(
                side=side, is_informed=False, confidence=0.4
            )
        if prev_mid is not None:
            side = "buy" if trade_price > prev_mid else "sell"
            return TradeClassification(
                side=side, is_informed=False, confidence=0.2
            )
        return TradeClassification(
            side="unknown", is_informed=False, confidence=0.0
        )


class VPINCalculator:
    """Volume-synchronized Probability of Informed Trading (VPIN).

    Follows Easley, O'Hara, and Srinivas (1998). VPIN measures the
    probability that a trade is initiated by an informed trader by
    comparing buy-initiated volume (BIP) and sell-initiated volume (SIP)
    over a rolling window of volume buckets.

    Standard approach:
    1. Accumulate trades into fixed-volume buckets (e.g., 1000 contracts)
    2. For each bucket, classify cumulative buy vs sell volume
    3. VPIN = |BIP - SIP| / (BIP + SIP) per bucket, averaged over window

    Attributes:
        bucket_volume: Number of contracts per volume bucket.
        window_buckets: Number of buckets in the rolling window.
    """

    def __init__(self, bucket_volume: int = 1000, window_buckets: int = 265) -> None:
        self.bucket_volume = bucket_volume
        self.window_buckets = window_buckets
        self._classifier = OrderFlowClassifier()
        # Current incomplete bucket
        self._current_buy_vol: float = 0.0
        self._current_sell_vol: float = 0.0
        # Rolling window of completed bucket VPINs
        self._bucket_vpins: collections.deque[float] = collections.deque(
            maxlen=window_buckets
        )
        # For tick-direction tracking
        self._prev_price: float | None = None

    def add_trade(
        self,
        price: float,
        size: int,
        bid_price: float | None = None,
        ask_price: float | None = None,
    ) -> None:
        """Add a single trade to the VPIN estimator."""
        # Determine tick direction
        tick_direction = None
        if self._prev_price is not None:
            tick_direction = 1 if price > self._prev_price else (-1 if price < self._prev_price else 0)
        prev_mid = None
        if bid_price is not None and ask_price is not None:
            prev_mid = (bid_price + ask_price) / 2.0

        classification = self._classifier.classify(
            price, bid_price, ask_price, prev_mid, tick_direction
        )
        self._prev_price = price

        # Add volume to the appropriate side
        if classification.side == "buy":
            self._current_buy_vol += size
        elif classification.side == "sell":
            self._current_sell_vol += size
        else:
            # Unknown — split evenly (conservative)
            half = size / 2.0
            self._current_buy_vol += half
            self._current_sell_vol += half

        # Check if bucket is full
        total = self._current_buy_vol + self._current_sell_vol
        while total >= self.bucket_volume:
            # Compute partial bucket contribution
            excess = total - self.bucket_volume
            # Full bucket VPIN
            bip = self._current_buy_vol
            sip = self._current_sell_vol
            bucket_total = bip + sip
            if bucket_total > 0:
                vpin = abs(bip - sip) / bucket_total
                self._bucket_vpins.append(vpin)
            # Reset with excess volume on the correct side
            if bip > sip:
                self._current_buy_vol = excess
                self._current_sell_vol = 0.0
            else:
                self._current_sell_vol = excess
                self._current_buy_vol = 0.0
            total = self._current_buy_vol + self._current_sell_vol

    @property
    def vpin(self) -> float:
        """Current VPIN value (0.0 to 1.0).

        0.5 = balanced flow (no toxicity), approaching 1.0 = highly toxic.
        """
        if not self._bucket_vpins:
            return 0.5
        return float(statistics.mean(self._bucket_vpins))

    @property
    def vpin_std(self) -> float:
        """Standard deviation of VPIN across buckets."""
        if len(self._bucket_vpins) < 2:
            return 0.0
        return float(statistics.pstdev(self._bucket_vpins))


class LiquidityMetrics:
    """Tracks liquidity metrics: spread, depth, persistence.

    Maintains rolling statistics on bid-ask spread and depth at the
    point of control (POC) for order book health monitoring.
    """

    def __init__(self, window: int = 100) -> None:
        self.window = window
        self._spreads_bps: collections.deque[float] = collections.deque(maxlen=window)
        self._depths: collections.deque[float] = collections.deque(maxlen=window)

    def add_spread(self, spread_bps: float | None) -> None:
        """Add a spread observation (in basis points)."""
        if spread_bps is not None:
            self._spreads_bps.append(spread_bps)

    def add_depth(self, depth: float | None) -> None:
        """Add a depth-at-POC observation."""
        if depth is not None:
            self._depths.append(depth)

    @property
    def spread_p50(self) -> float:
        if not self._spreads_bps:
            return 0.0
        return float(statistics.median(self._spreads_bps))

    @property
    def spread_p95(self) -> float:
        if not self._spreads_bps:
            return 0.0
        sorted_spreads = sorted(self._spreads_bps)
        idx = int(len(sorted_spreads) * 0.95)
        return float(sorted_spreads[min(idx, len(sorted_spreads) - 1)])

    @property
    def spread_persistence(self) -> float:
        """Autocorrelation of spread at lag-1 (persistence of wide/narrow spreads).

        Returns 0.0 if insufficient data. Higher values indicate spread
        tends to persist across observations.
        """
        if len(self._spreads_bps) < 3:
            return 0.0
        values = list(self._spreads_bps)
        n = len(values)
        mean = statistics.mean(values)
        var = statistics.pvariance(values)
        if var == 0:
            return 0.0
        # Lag-1 autocorrelation
        cov = sum((values[i] - mean) * (values[i - 1] - mean) for i in range(1, n))
        return float(cov / (n - 1) / var)

    @property
    def depth_at_poc_median(self) -> float:
        if not self._depths:
            return 0.0
        return float(statistics.median(self._depths))


@dataclass
class FlowMetrics:
    """Aggregated microstructure metrics for a symbol.

    Bundles order flow classification, VPIN, and liquidity metrics
    into a single object that can be serialized and used in CEL rules.
    """
    symbol: str
    vpin: VPINCalculator = field(default_factory=lambda: VPINCalculator())
    liquidity: LiquidityMetrics = field(default_factory=lambda: LiquidityMetrics())
    trade_classifier: OrderFlowClassifier = field(default_factory=lambda: OrderFlowClassifier())
    prev_mid_price: float | None = None
    prev_mid_time: float | None = None  # epoch seconds

    def update(
        self,
        snapshot: ConsolidatedSnapshot,
        trade_price: float,
        trade_size: int,
    ) -> None:
        """Update all microstructure metrics from a new trade + snapshot state."""
        bid = float(snapshot.bid_price) if snapshot.bid_price else None
        ask = float(snapshot.ask_price) if snapshot.ask_price else None

        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
            self.prev_mid_price = mid

            # Spread in bps
            if mid and mid > 0:
                spread_bps = float((ask - bid) / mid * 10000)
                self.liquidity.add_spread(spread_bps)

        # Update VPIN
        self.vpin.add_trade(trade_price, trade_size, bid, ask)


@dataclass
class CrossAssetFlowState:
    """Tracks cross-asset volume flow for lead-lag and correlation analysis.

    Records per-symbol trade volumes over time to enable:
    - Lead-lag analysis (does SPY volume lead QQQ?)
    - Cross-asset anomaly aggregation (systemic flow index)
    """
    symbol: str
    volumes: collections.deque = field(default_factory=lambda: collections.deque(maxlen=1000))
    timestamps: collections.deque = field(default_factory=lambda: collections.deque(maxlen=1000))

    def add_trade(self, size: int, timestamp: float) -> None:
        self.volumes.append(size)
        self.timestamps.append(timestamp)

    @property
    def recent_volume(self) -> float:
        if not self.volumes:
            return 0.0
        return sum(self.volumes)


@dataclass
class CrossAssetHawkes:
    """Multivariate Hawkes process for cross-asset excitation.

    Models how trade events in one symbol (e.g., SPY) excite the
    intensity of events in correlated symbols (e.g., QQQ, ES).

    Simplified implementation: tracks excitation matrix where
    mu_i + sum_j(alpha_ij * sum_past(phi(t - t_p)))

    Attributes:
        symbols: List of symbols being tracked.
        mu: Baseline intensity per symbol.
        alpha: Excitation matrix [from_symbol][to_symbol] -> excitation factor.
        decay: Exponential decay rate for excitation.
    """
    symbols: list[str]
    mu: float = 0.1
    alpha: dict[str, dict[str, float]] = field(default_factory=dict)
    decay: float = 1.0
    _last_events: dict[str, float] = field(default_factory=dict)
    _intensities: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.alpha:
            # Initialize all-to-all excitation (symmetric, default 0.5)
            self.alpha = {s: {t: 0.5 for t in self.symbols} for s in self.symbols}
        if not self._intensities:
            self._intensities = {s: self.mu for s in self.symbols}
        if not self._last_events:
            self._last_events = {s: 0.0 for s in self.symbols}

    def add_event(self, symbol: str, timestamp: float) -> None:
        """Add a trade event for a symbol, updating all intensities."""
        self._last_events[symbol] = timestamp
        # Update intensities: each symbol gets excited by the triggering symbol
        for target in self.symbols:
            if target == symbol:
                # Self-excitation
                excitation = self.alpha[symbol][symbol] * self.decay
            else:
                # Cross-excitation
                excitation = self.alpha[symbol][target] * self.decay
            self._intensities[target] += excitation
        # Decay all intensities
        for s in self.symbols:
            self._intensities[s] = self.mu + (self._intensities[s] - self.mu) * math.exp(-self.decay * 0.01)

    def expected_events(self, symbol: str, horizon: float = 60.0) -> float:
        """Expected number of events in the next `horizon` seconds."""
        intensity = self._intensities.get(symbol, self.mu)
        return intensity * horizon

    def systemic_anomaly_score(self) -> float:
        """Aggregate anomaly score across all symbols.

        Returns a value > 1.0 when cross-asset excitation is elevated
        (systemic flow detected).
        """
        if not self._intensities:
            return 1.0
        avg_intensity = sum(self._intensities.values()) / len(self._intensities)
        if self.mu > 0:
            return avg_intensity / self.mu
        return 1.0

    def to_dict(self) -> dict:
        return {
            "symbols": list(self.symbols),
            "mu": self.mu,
            "alpha": self.alpha,
            "decay": self.decay,
            "_last_events": dict(self._last_events),
            "_intensities": dict(self._intensities),
        }

    @classmethod
    def from_dict(cls, data: dict) -> CrossAssetHawkes:
        obj = cls(
            symbols=data.get("symbols", []),
            mu=data.get("mu", 0.1),
            alpha=data.get("alpha", {}),
            decay=data.get("decay", 1.0),
        )
        obj._last_events = dict(data.get("_last_events", {}))
        obj._intensities = dict(data.get("_intensities", {}))
        return obj


def compute_lead_lag(
    symbol_a: str,
    symbol_b: str,
    flows_a: CrossAssetFlowState,
    flows_b: CrossAssetFlowState,
    lag_steps: int = 10,
) -> dict:
    """Compute cross-correlation of volume flows at various lags.

    Args:
        symbol_a: Leading symbol.
        symbol_b: Lagging symbol.
        flows_a: Volume flow state for symbol A.
        flows_b: Volume flow state for symbol B.
        lag_steps: Maximum lag to test (in event index units).

    Returns:
        Dict with best_lag, correlation, and lead_sign.
    """
    vols_a = list(flows_a.volumes)
    vols_b = list(flows_b.volumes)

    if len(vols_a) < lag_steps or len(vols_b) < lag_steps:
        return {
            "symbol_a": symbol_a,
            "symbol_b": symbol_b,
            "best_lag": 0,
            "correlation": 0.0,
            "lead_sign": "insufficient_data",
        }

    max_lag = min(lag_steps, len(vols_a) - 1, len(vols_b) - 1)
    best_corr = 0.0
    best_lag = 0

    for lag in range(1, max_lag + 1):
        if len(vols_a) <= lag or len(vols_b) <= lag:
            continue
        a = vols_a[:len(vols_a) - lag] if len(vols_a) > lag else vols_a
        b = vols_b[lag:lag + len(a)] if len(vols_b) > lag + len(a) else vols_b[:len(a)]
        if len(a) < 2 or len(b) < 2 or len(a) != len(b):
            continue
        try:
            mean_a = sum(a) / len(a)
            mean_b = sum(b) / len(b)
            cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(len(a))) / len(a)
            std_a = math.sqrt(sum((x - mean_a) ** 2 for x in a) / len(a)) if a else 0.0
            std_b = math.sqrt(sum((x - mean_b) ** 2 for x in b) / len(b)) if b else 0.0
            if std_a > 0 and std_b > 0:
                corr = cov / (std_a * std_b)
                if abs(corr) > abs(best_corr):
                    best_corr = corr
                    best_lag = lag
        except (ZeroDivisionError, ValueError):
            continue

    return {
        "symbol_a": symbol_a,
        "symbol_b": symbol_b,
        "best_lag": best_lag,
        "correlation": float(best_corr),
        "lead_sign": "positive" if best_corr > 0 else ("negative" if best_corr < 0 else "none"),
    }
