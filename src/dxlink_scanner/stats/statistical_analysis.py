"""Statistical analysis module for options volume scanner.

Provides advanced statistical methods for anomaly detection, regime classification,
and significance testing with proper statistical foundations.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field

import numpy as np
from scipy import stats as scipy_stats


@dataclass(slots=True)
class BayesianGammaPoisson:
    """Bayesian Gamma-Poisson (Negative Binomial) conjugate model for trade counts.

    Models trade arrivals as Poisson process with Gamma-distributed rate.
    Enables online updating of posterior and credible intervals.
    """

    # Prior parameters (Gamma distribution: shape=alpha, rate=beta)
    alpha: float = 1.0
    beta: float = 1.0

    # Posterior parameters (updated after observations)
    alpha_post: float = 1.0
    beta_post: float = 1.0

    # Sufficient statistics
    n_observations: int = 0
    sum_counts: int = 0

    def update(self, count: int, exposure: float = 1.0) -> None:
        """Update posterior with new observation.

        Args:
            count: Number of trades observed
            exposure: Time/exposure weight (default 1.0 for one interval)
        """
        self.n_observations += 1
        self.sum_counts += count
        self.alpha_post = self.alpha + self.sum_counts
        self.beta_post = self.beta + self.n_observations * exposure

    def posterior_mean(self) -> float:
        """Expected rate (trades per interval)."""
        return self.alpha_post / self.beta_post

    def posterior_mode(self) -> float:
        """MAP estimate of rate."""
        return (self.alpha_post - 1) / self.beta_post if self.alpha_post > 1 else 0.0

    def credible_interval(self, confidence: float = 0.95) -> tuple[float, float]:
        """Equal-tailed credible interval for rate."""
        lower = scipy_stats.gamma.ppf((1 - confidence) / 2, self.alpha_post, scale=1 / self.beta_post)
        upper = scipy_stats.gamma.ppf((1 + confidence) / 2, self.alpha_post, scale=1 / self.beta_post)
        return (lower, upper)

    def predictive_prob(self, k: int, exposure: float = 1.0) -> float:
        """Posterior predictive probability of observing k trades."""
        # Negative Binomial: NB(r=alpha_post, p=beta_post/(beta_post+exposure))
        r = self.alpha_post
        p = self.beta_post / (self.beta_post + exposure)
        return scipy_stats.nbinom.pmf(k, r, p)

    def p_value(self, observed: int, exposure: float = 1.0, alternative: str = "greater") -> float:
        """Bayesian p-value (tail probability) for observed count."""
        if alternative == "greater":
            return sum(self.predictive_prob(k, exposure) for k in range(observed, observed + 50))
        elif alternative == "less":
            return sum(self.predictive_prob(k, exposure) for k in range(0, observed + 1))
        else:  # two-sided
            mean = self.posterior_mean() * exposure
            if observed > mean:
                return 2 * sum(self.predictive_prob(k, exposure) for k in range(observed, observed + 50))
            else:
                return 2 * sum(self.predictive_prob(k, exposure) for k in range(0, observed + 1))

    def reset(self) -> None:
        """Reset to prior."""
        self.alpha_post = self.alpha
        self.beta_post = self.beta
        self.n_observations = 0
        self.sum_counts = 0


@dataclass(slots=True)
class HawkesProcess:
    """Hawkes self-exciting point process for trade clustering detection.

    Models trade arrivals where each event increases the conditional intensity,
    capturing the "burstiness" of market activity.

    Intensity: λ(t) = μ + Σ α * exp(-β * (t - t_i)) for all t_i < t
    """

    # Base intensity (background rate)
    mu: float = 0.1
    # Excitation magnitude
    alpha: float = 0.5
    # Decay rate
    beta: float = 1.0

    # Event history
    _event_times: list[float] = field(default_factory=list)
    _current_intensity: float = 0.0

    def add_event(self, timestamp: float) -> None:
        """Add event at timestamp (epoch seconds)."""
        self._event_times.append(timestamp)
        self._update_intensity(timestamp)

    def _update_intensity(self, t: float) -> None:
        """Compute current conditional intensity."""
        if not self._event_times:
            self._current_intensity = self.mu
            return

        intensity = self.mu
        for ti in self._event_times:
            if ti < t:
                intensity += self.alpha * math.exp(-self.beta * (t - ti))
        self._current_intensity = intensity

    def current_intensity(self, timestamp: float | None = None) -> float:
        """Get current intensity at timestamp (or now)."""
        t = timestamp or dt.datetime.now(dt.UTC).timestamp()
        self._update_intensity(t)
        return self._current_intensity

    def expected_events(self, window_sec: float, from_time: float | None = None) -> float:
        """Expected number of events in future window."""
        t0 = from_time or dt.datetime.now(dt.UTC).timestamp()
        # For Hawkes, expected = integral of intensity
        # Approximate: μ * T + (α/β) * (current_excitation) * (1 - exp(-β * T))
        T = window_sec
        excitation = sum(self.alpha * math.exp(-self.beta * (t0 - ti)) for ti in self._event_times if ti < t0)
        return self.mu * T + excitation * (1 - math.exp(-self.beta * T))

    def clustering_p_value(self, observed: int, window_sec: float = 60.0) -> float:
        """Test if observed count shows significant clustering vs Poisson."""
        expected = self.expected_events(window_sec)
        # Under Poisson null with rate = expected/window
        poisson_rate = expected / window_sec * window_sec  # = expected
        # P(observed or more under Poisson)
        return 1 - scipy_stats.poisson.cdf(observed - 1, poisson_rate)

    def fit_simple(self, timestamps: list[float]) -> None:
        """Simple method-of-moments fit for μ, α, β."""
        if len(timestamps) < 10:
            return

        # Inter-event times
        intervals = np.diff(timestamps)
        mean_interval = np.mean(intervals)
        _ = np.var(intervals)  # For potential future use

        # For Hawkes: E[T] = 1/μ_eff where μ_eff = μ/(1 - α/β)
        # Method of moments estimation
        self.mu = max(0.01, 1.0 / mean_interval)
        # Assume β = 2/mean_interval (half-life ~ mean interval)
        self.beta = 2.0 / mean_interval
        # Assume α = β * 0.3 (30% branching ratio)
        self.alpha = self.beta * 0.3


@dataclass(slots=True)
class TimeOfDaySeasonality:
    """Time-of-day seasonality model for intraday volume patterns.

    Models expected volume as piecewise-constant or spline function of time.
    Useful for normalizing volume by expected intraday pattern.
    """

    # Bin edges in minutes from midnight ET (e.g., 570=9:30, 960=16:00)
    bin_edges: list[int] = field(default_factory=lambda: list(range(570, 961, 30)))
    # Observed volumes per bin
    _bin_counts: dict[int, list[float]] = field(default_factory=dict)
    _bin_means: dict[int, float] = field(default_factory=dict)
    _global_mean: float = 1.0

    def add_observation(self, timestamp: dt.datetime, volume: float) -> None:
        """Add volume observation at timestamp."""
        # Convert to ET minutes from midnight
        et = timestamp.astimezone(dt.timezone(dt.timedelta(hours=-4)))  # Approximate ET
        minutes = et.hour * 60 + et.minute

        # Find bin
        bin_idx = 0
        for i, edge in enumerate(self.bin_edges):
            if minutes < edge:
                bin_idx = i - 1 if i > 0 else 0
                break
        else:
            bin_idx = len(self.bin_edges) - 1

        bin_edge = self.bin_edges[bin_idx] if bin_idx < len(self.bin_edges) else self.bin_edges[-1]
        self._bin_counts.setdefault(bin_edge, []).append(volume)
        self._recompute()

    def _recompute(self) -> None:
        """Recompute bin means and global mean."""
        all_volumes = []
        for bin_edge, volumes in self._bin_counts.items():
            self._bin_means[bin_edge] = statistics.mean(volumes)
            all_volumes.extend(volumes)
        self._global_mean = statistics.mean(all_volumes) if all_volumes else 1.0

    def expected_volume(self, timestamp: dt.datetime) -> float:
        """Expected volume at timestamp."""
        et = timestamp.astimezone(dt.timezone(dt.timedelta(hours=-4)))
        minutes = et.hour * 60 + et.minute

        bin_idx = 0
        for i, edge in enumerate(self.bin_edges):
            if minutes < edge:
                bin_idx = i - 1 if i > 0 else 0
                break
        else:
            bin_idx = len(self.bin_edges) - 1

        bin_edge = self.bin_edges[bin_idx] if bin_idx < len(self.bin_edges) else self.bin_edges[-1]
        return self._bin_means.get(bin_edge, self._global_mean)

    def seasonality_factor(self, timestamp: dt.datetime) -> float:
        """Multiplicative factor: expected/globally_expected."""
        expected = self.expected_volume(timestamp)
        return expected / self._global_mean if self._global_mean > 0 else 1.0

    def normalized_volume(self, timestamp: dt.datetime, observed: float) -> float:
        """Volume normalized by seasonal expectation."""
        factor = self.seasonality_factor(timestamp)
        return observed / factor if factor > 0 else observed


@dataclass(slots=True)
class CrossSymbolPool:
    """Hierarchical pooling across symbols for sparse data.

    Shares information across related symbols (same underlying) using
    empirical Bayes / hierarchical Bayesian approach.
    """

    # Global hyperpriors
    global_alpha: float = 1.0
    global_beta: float = 1.0

    # Per-symbol posteriors
    symbol_posteriors: dict[str, BayesianGammaPoisson] = field(default_factory=dict)

    def update_symbol(self, symbol: str, count: int, exposure: float = 1.0) -> None:
        """Update a symbol's posterior."""
        if symbol not in self.symbol_posteriors:
            self.symbol_posteriors[symbol] = BayesianGammaPoisson(
                alpha=self.global_alpha, beta=self.global_beta
            )
        self.symbol_posteriors[symbol].update(count, exposure)

    def get_pooled_estimate(self, symbol: str, shrinkage: float = 0.5) -> float:
        """Get shrinkage estimate toward global mean."""
        if symbol not in self.symbol_posteriors:
            return self.global_alpha / self.global_beta

        post = self.symbol_posteriors[symbol]
        local_mean = post.posterior_mean()
        global_mean = self.global_alpha / self.global_beta

        # Shrinkage weight based on observation count
        n = post.n_observations
        weight = n / (n + 1 / shrinkage) if n > 0 else 0.0
        return weight * local_mean + (1 - weight) * global_mean

    def credible_interval(self, symbol: str, confidence: float = 0.95) -> tuple[float, float]:
        """Get credible interval with pooling."""
        if symbol not in self.symbol_posteriors:
            return (0.0, scipy_stats.gamma.ppf(confidence, self.global_alpha, scale=1 / self.global_beta))

        post = self.symbol_posteriors[symbol]
        return post.credible_interval(confidence)


@dataclass(slots=True)
class VolumeAtPrice:
    """Volume-at-Price (VAP) profile for order flow analysis.

    Aggregates volume by price level to identify support/resistance and
    value areas (where most volume traded).
    """

    tick_size: float = 0.01
    _price_volume: dict[int, float] = field(default_factory=dict)  # price in ticks -> volume
    _total_volume: float = 0.0

    def add_trade(self, price: float, size: int) -> None:
        """Add trade at price."""
        tick = int(round(price / self.tick_size))
        self._price_volume[tick] = self._price_volume.get(tick, 0.0) + size
        self._total_volume += size

    def get_vap(self) -> dict[float, float]:
        """Return price -> volume mapping."""
        return {tick * self.tick_size: vol for tick, vol in self._price_volume.items()}

    def value_area(self, pct: float = 0.70) -> tuple[float, float]:
        """Price range containing pct% of volume (TPO-style)."""
        if not self._price_volume:
            return (0.0, 0.0)

        # Sort by volume descending
        sorted_levels = sorted(self._price_volume.items(), key=lambda x: x[1], reverse=True)
        target = self._total_volume * pct
        cum = 0.0
        prices = []

        for tick, vol in sorted_levels:
            cum += vol
            prices.append(tick * self.tick_size)
            if cum >= target:
                break

        return (min(prices), max(prices))

    def poc(self) -> float:
        """Point of Control - price with highest volume."""
        if not self._price_volume:
            return 0.0
        max_tick = max(self._price_volume.items(), key=lambda x: x[1])[0]
        return max_tick * self.tick_size

    def imbalance(self, lookback_ticks: int = 5) -> float:
        """Order flow imbalance: (buy vol - sell vol) / (buy vol + sell vol) near POC."""
        poc_tick = int(round(self.poc() / self.tick_size))
        buy_vol = 0.0
        sell_vol = 0.0

        # Approximation: trades above POC = buy pressure, below = sell
        for tick, vol in self._price_volume.items():
            if abs(tick - poc_tick) <= lookback_ticks:
                if tick > poc_tick:
                    buy_vol += vol
                elif tick < poc_tick:
                    sell_vol += vol

        total = buy_vol + sell_vol
        return (buy_vol - sell_vol) / total if total > 0 else 0.0


@dataclass(slots=True)
class RegimeState:
    """Market regime classification result."""

    regime: int  # 0=low_vol, 1=normal, 2=high_vol, 3=crash
    probability: float
    volatility: float
    volume_rate: float


class RegimeDetector:
    """Simple regime detection using volatility + volume thresholds.

    More sophisticated version could use HMM.
    """

    def __init__(
        self,
        vol_low: float = 0.01,
        vol_high: float = 0.03,
        vol_crash: float = 0.05,
        vol_window: int = 50,
    ):
        self.vol_low = vol_low
        self.vol_high = vol_high
        self.vol_crash = vol_crash
        self.vol_window = vol_window
        self._returns: list[float] = []
        self._volumes: list[int] = []

    def update(self, price: float, volume: int) -> None:
        """Update with new price and volume."""
        if len(self._returns) > 0:
            ret = math.log(price / self._returns[-1]) if self._returns[-1] > 0 else 0.0
            self._returns.append(ret)
        else:
            self._returns.append(price)
        self._volumes.append(volume)

        if len(self._returns) > self.vol_window:
            self._returns.pop(0)
            self._volumes.pop(0)

    def detect(self) -> RegimeState:
        """Classify current regime."""
        if len(self._returns) < 10:
            return RegimeState(regime=1, probability=1.0, volatility=0.0, volume_rate=0.0)

        # Volatility (std of log returns)
        returns = np.array(self._returns[-self.vol_window:])
        vol = float(np.std(returns)) if len(returns) > 1 else 0.0

        # Volume rate
        vol_rate = float(np.mean(self._volumes[-self.vol_window:])) if self._volumes else 0.0

        # Classify
        if vol >= self.vol_crash:
            regime = 3  # crash
            prob = min(1.0, vol / self.vol_crash)
        elif vol >= self.vol_high:
            regime = 2  # high vol
            prob = (vol - self.vol_low) / (self.vol_high - self.vol_low)
        elif vol >= self.vol_low:
            regime = 1  # normal
            prob = 1.0 - (vol - self.vol_low) / (self.vol_high - self.vol_low)
        else:
            regime = 0  # low vol
            prob = 1.0 - vol / self.vol_low if self.vol_low > 0 else 1.0

        return RegimeState(regime=regime, probability=prob, volatility=vol, volume_rate=vol_rate)


def false_discovery_rate_control(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg FDR control.

    Returns boolean mask of which hypotheses to reject.
    """
    if not p_values:
        return []

    n = len(p_values)
    sorted_indices = sorted(range(n), key=lambda i: p_values[i])
    sorted_p = [p_values[i] for i in sorted_indices]

    rejected = [False] * n
    for i, p in enumerate(sorted_p):
        threshold = (i + 1) / n * alpha
        if p <= threshold:
            rejected[sorted_indices[i]] = True
        else:
            break  # BH procedure: stop at first non-rejection

    return rejected


def bayesian_anomaly_score(
    observed: int,
    posterior: BayesianGammaPoisson,
    exposure: float = 1.0,
) -> dict[str, float]:
    """Compute comprehensive Bayesian anomaly metrics.

    Returns dict with: p_value, posterior_mean, credible_interval, Bayes_factor
    """
    pred_mean = posterior.posterior_mean() * exposure
    p_val = posterior.p_value(observed, exposure, "greater")

    # Bayes factor vs null of "typical" rate (posterior mean)
    null_rate = pred_mean
    alt_rate = posterior.posterior_mean() * 2  # 2x typical

    # Likelihood ratio
    if null_rate > 0 and alt_rate > 0:
        bf = (scipy_stats.poisson.pmf(observed, alt_rate) /
              scipy_stats.poisson.pmf(observed, null_rate))
    else:
        bf = 1.0

    ci = posterior.credible_interval(0.95)

    return {
        "p_value": p_val,
        "posterior_mean": pred_mean,
        "observed": observed,
        "ratio": observed / pred_mean if pred_mean > 0 else 0.0,
        "credible_interval_low": ci[0] * exposure,
        "credible_interval_high": ci[1] * exposure,
        "bayes_factor": bf,
        "significant": p_val < 0.05 and observed > pred_mean,
    }

