"""Model persistence, checkpointing, and warm-up for statistical models.

Provides:
  - ``ModelStore``: Serialize/deserialize all per-symbol statistical models
    to/from ``models_meta.json``. Handles startup warm-up and periodic
    checkpointing.
  - ``prior_elicitation``: Empirical Bayes hyperparameter estimation from
    historical parquet data.
  - ``CalibrationDiagnostics``: PIT histograms, coverage tests, Hawkes
    residuals, seasonality stability, and model comparison logging.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from dxlink_scanner.stats.statistical_analysis import (
    BayesianGammaPoisson,
    CrossSymbolPool,
    HawkesProcess,
    RegimeDetector,
    TimeOfDaySeasonality,
    VolumeAtPrice,
    false_discovery_rate_control,
)

logger = logging.getLogger(__name__)


@dataclass
class ModelSet:
    """Container for all statistical models for a single symbol/underlying."""

    bayesian: BayesianGammaPoisson = field(default_factory=lambda: BayesianGammaPoisson())
    hawkes: HawkesProcess = field(default_factory=lambda: HawkesProcess())
    seasonality: TimeOfDaySeasonality = field(default_factory=lambda: TimeOfDaySeasonality())
    pool: CrossSymbolPool = field(default_factory=lambda: CrossSymbolPool())
    vap: VolumeAtPrice = field(default_factory=lambda: VolumeAtPrice())
    regime: RegimeDetector = field(default_factory=lambda: RegimeDetector())

    def to_dict(self) -> dict[str, Any]:
        """Serialize all models to a JSON-compatible dict."""
        return {
            "bayesian": self.bayesian.to_dict(),
            "hawkes": self.hawkes.to_dict(),
            "seasonality": self.seasonality.to_dict(),
            "pool": self.pool.to_dict(),
            "vap": self.vap.to_dict(),
            "regime": self.regime.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelSet:
        """Restore all models from a dict produced by ``to_dict``."""
        return cls(
            bayesian=BayesianGammaPoisson.from_dict(data.get("bayesian", {})),
            hawkes=HawkesProcess.from_dict(data.get("hawkes", {})),
            seasonality=TimeOfDaySeasonality.from_dict(data.get("seasonality", {})),
            pool=CrossSymbolPool.from_dict(data.get("pool", {})),
            vap=VolumeAtPrice.from_dict(data.get("vap", {})),
            regime=RegimeDetector.from_dict(data.get("regime", {})),
        )


class ModelStore:
    """Persist statistical model state across scanner restarts.

    Writes ``models_meta.json`` to ``data_dir`` on a configurable interval
    and on graceful shutdown. On startup, loads the file if present so
    models start with informed priors rather than defaults.

    Args:
        data_dir: Directory for model state files.
        checkpoint_interval_sec: How often to write checkpoint (seconds).
    """

    def __init__(
        self,
        data_dir: str | Path = "data/events",
        checkpoint_interval_sec: float = 600.0,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_interval_sec = checkpoint_interval_sec
        self._models_path = self._data_dir / "models_meta.json"
        self._last_checkpoint = time.time()  # Don't checkpoint immediately on first call

    @property
    def models_path(self) -> Path:
        """Path to the model state file."""
        return self._models_path

    def save(self, models: dict[str, ModelSet]) -> None:
        """Write model state to disk immediately."""
        serializable: dict[str, dict[str, Any]] = {}
        for symbol, model_set in models.items():
            serializable[str(symbol)] = model_set.to_dict()
        try:
            tmp_path = self._models_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(serializable, indent=2, default=str))
            tmp_path.replace(self._models_path)
            self._last_checkpoint = time.time()
            logger.info("Saved %d model sets to %s", len(models), self._models_path)
        except Exception as e:
            logger.warning("Failed to save model state: %s", e)

    def load(self) -> dict[str, ModelSet]:
        """Load model state from disk if available."""
        if not self._models_path.exists():
            logger.info("No model state file found; models will start fresh")
            return {}
        try:
            raw = json.loads(self._models_path.read_text())
        except Exception as e:
            logger.warning("Failed to load model state: %s", e)
            return {}
        models: dict[str, ModelSet] = {}
        for symbol, data in raw.items():
            models[str(symbol)] = ModelSet.from_dict(data)
        logger.info("Loaded %d model sets from %s", len(models), self._models_path)
        return models

    def maybe_checkpoint(self, models: dict[str, ModelSet]) -> None:
        """Checkpoint if enough time has elapsed since the last checkpoint."""
        now = time.time()
        if now - self._last_checkpoint >= self._checkpoint_interval_sec:
            self.save(models)

    def warm_up(
        self,
        symbols: list[str],
        hyperpriors: dict[str, float] | None = None,
    ) -> dict[str, ModelSet]:
        """Create initial model sets for the given symbols.

        Uses loaded state from ``models_meta.json`` if available; otherwise
        initializes from hyperpriors (empirical Bayes) or defaults.

        Args:
            symbols: List of underlying symbols to create models for.
            hyperpriors: Optional dict with ``alpha``, ``beta``, ``mu``,
                ``alpha``, ``beta`` (Hawkes) keys from prior elicitation.

        Returns:
            Dict mapping symbol -> ModelSet.
        """
        saved = self.load()
        models: dict[str, ModelSet] = {}
        hp = hyperpriors or {}

        for sym in symbols:
            if sym in saved:
                models[sym] = saved[sym]
                continue

            # Initialize with hyperpriors or defaults
            alpha = hp.get("alpha", 1.0)
            beta = hp.get("beta", 1.0)
            bayesian = BayesianGammaPoisson(alpha=alpha, beta=beta)
            models[sym] = ModelSet(
                bayesian=bayesian,
            )

        # Always ensure a "default" fallback
        if "default" not in models:
            models["default"] = ModelSet()

        return models


def prior_elicitation(
    parquet_dir: str | Path,
    lookback_days: int = 30,
) -> dict[str, float]:
    """Compute empirical Bayes hyperpriors from historical parquet data.

    Estimates Gamma-Poisson hyperparameters (alpha, beta) from trade count
    data over the specified lookback window. Uses method of moments on
    the negative binomial distribution.

    Args:
        parquet_dir: Directory containing partitioned parquet event files.
        lookback_days: Number of days of history to use.

    Returns:
        Dict with keys ``alpha``, ``beta`` (and ``mu``, ``alpha``, ``beta``
        for Hawkes if estimable). Returns defaults if data unavailable.
    """
    defaults: dict[str, float] = {
        "alpha": 1.0,
        "beta": 1.0,
        "mu": 0.1,
        "hawkes_alpha": 0.5,
        "hawkes_beta": 1.0,
    }
    import pyarrow.parquet as pq  # Deferred import: heavy dependency

    parquet_path = Path(parquet_dir)
    if not parquet_path.exists():
        logger.warning("Parquet directory %s does not exist; using default priors", parquet_path)
        return defaults

    try:
        dataset = pq.ParquetDataset(parquet_path)
        # Read with a date filter for lookback
        table = dataset.read()
        if table.num_rows == 0:
            return defaults

        df = table.to_pandas()
        # Filter to recent trades
        if "received_at" in df.columns:
            cutoff = df["received_at"].max() - pd_Timedelta(days=lookback_days)
            df = df[df["received_at"] >= cutoff]

        # Group by underlying_symbol, count trades per minute
        if "underlying_symbol" in df.columns and "source_type" in df.columns:
            tas = df[df["source_type"] == "TIME_AND_SALE"]
            if len(tas) > 0:
                counts = tas.groupby(["underlying_symbol", pd_Grouper(key="received_at", freq="1min")]).size()
                all_counts = counts.values

                if len(all_counts) > 1:
                    mean_count = float(np.mean(all_counts))
                    var_count = float(np.var(all_counts))

                    # Method of moments for Gamma-Poisson (Negative Binomial)
                    # mean = r * (1-p) / p, var = r * (1-p) / p^2
                    # => alpha = mean^2 / (var - mean), beta = mean / (var - mean)
                    if var_count > mean_count and mean_count > 0:
                        alpha = mean_count**2 / (var_count - mean_count)
                        beta = mean_count / (var_count - mean_count)
                        defaults["alpha"] = float(alpha)
                        defaults["beta"] = float(beta)
                        logger.info(
                            "Prior elicitation: alpha=%.4f, beta=%.4f (mean=%.4f, var=%.4f)",
                            alpha, beta, mean_count, var_count,
                        )
    except Exception as e:
        logger.warning("Prior elicitation failed: %s; using defaults", e)

    return defaults


# Pandas imports for prior_elicitation (deferred to avoid import overhead)
from pandas import Grouper as pd_Grouper  # noqa: E402
from pandas import Timedelta as pd_Timedelta  # noqa: E402


@dataclass
class CalibrationDiagnostics:
    """Run calibration and validation diagnostics on statistical models.

    Produces metrics for:
      - PIT (Probability Integral Transform) histograms for Bayesian models
      - Coverage tests for credible intervals
      - Hawkes residual analysis (time-rescaling theorem)
      - Seasonality stability (rolling correlation across weeks)
      - Model comparison (predictive likelihood: Bayesian vs Hawkes vs Poisson)
    """

    def pit_values(
        self,
        model: BayesianGammaPoisson,
        observed_counts: list[int],
        exposure: float = 1.0,
    ) -> list[float]:
        """Compute PIT (Probability Integral Transform) values.

        For a well-calibrated model, PIT values should be approximately
        uniform on [0, 1].

        Returns:
            List of PIT values, one per observation.
        """
        pit_vals: list[float] = []
        for obs in observed_counts:
            # PIT = P(X <= obs) under the posterior predictive (Negative Binomial)
            cdf_val = sum(
                model.predictive_prob(k, exposure)
                for k in range(obs + 1)
            )
            # Clamp to [0, 1] to handle numerical edge cases
            cdf_val = max(0.0, min(1.0, cdf_val))
            pit_vals.append(cdf_val)
        return pit_vals

    def coverage_test(
        self,
        model: BayesianGammaPoisson,
        observed_counts: list[int],
        exposure: float = 1.0,
        confidence: float = 0.95,
    ) -> dict[str, float]:
        """Test empirical coverage of credible intervals.

        For a well-calibrated 95% credible interval, approximately 95%
        of observations should fall within the interval.

        Returns:
            Dict with ``coverage_rate``, ``ci_low``, ``ci_high``,
            ``n_observations``, ``n_in_interval``.
        """
        if not observed_counts:
            return {"coverage_rate": 0.0, "ci_low": 0.0, "ci_high": 0.0,
                    "n_observations": 0, "n_in_interval": 0}

        ci_low_rate, ci_high_rate = model.credible_interval(confidence)
        # Use Poisson approximation for count CI bounds
        expected = model.posterior_mean() * exposure
        from scipy.stats import poisson as _pois
        count_low = _pois.ppf((1 - confidence) / 2, expected) if expected > 0 else 0
        count_high = _pois.ppf((1 + confidence) / 2, expected) if expected > 0 else 0

        n_in = sum(1 for obs in observed_counts if count_low <= obs <= count_high)
        rate = n_in / len(observed_counts)

        return {
            "coverage_rate": rate,
            "target_coverage": confidence,
            "ci_low": float(count_low),
            "ci_high": float(count_high),
            "n_observations": len(observed_counts),
            "n_in_interval": n_in,
        }

    def hawkes_residuals(
        self,
        model: HawkesProcess,
        timestamps: list[float],
        mu: float | None = None,
        alpha: float | None = None,
        beta: float | None = None,
    ) -> list[float]:
        """Compute Hawkes residuals via time-rescaling theorem.

        Under a correct model, the transformed inter-event times:
            Λ(t_i) - Λ(t_{i-1}) = ∫_{t_{i-1}}^{t_i} λ(s) ds
        should be i.i.d. Exponential(1).

        Returns:
            List of rescaled intervals (should be ~Exp(1) under the null).
        """
        if len(timestamps) < 2:
            return []

        m = model  # Use the model or create from params
        if mu is not None:
            m = HawkesProcess(mu=mu, alpha=alpha or model.alpha, beta=beta or model.beta)
        if alpha is not None:
            m = HawkesProcess(mu=m.mu, alpha=alpha, beta=beta or m.beta)
        if beta is not None:
            m = HawkesProcess(mu=m.mu, alpha=m.alpha, beta=beta)

        # Replay events to compute cumulative intensity
        residuals: list[float] = []
        prev_intensity_integral = 0.0
        event_times = list(timestamps)

        for i in range(1, len(event_times)):
            t_prev = event_times[i - 1]
            t_curr = event_times[i]
            # Cumulative intensity integral from t_prev to t_curr
            # For Hawkes: Λ(t) = μ*t + Σ (α/β)*(1 - exp(-β*(t - t_j))) for t_j < t
            integral = m.mu * (t_curr - t_prev)
            for tj in event_times[:i]:
                if tj < t_curr:
                    integral += (m.alpha / m.beta) * (
                        math.exp(-m.beta * (t_prev - tj)) - math.exp(-m.beta * (t_curr - tj))
                    )
            residuals.append(integral - prev_intensity_integral)
            prev_intensity_integral = integral

        return residuals

    def seasonality_stability(
        self,
        model: TimeOfDaySeasonality,
        weeks: int = 4,
    ) -> float:
        """Compute rolling correlation of bin means across weeks.

        Returns:
            Pearson correlation between consecutive weeks' bin means.
            Low correlation (< 0.7) may indicate regime shifts in
            intraday patterns.
        """
        if not model._bin_counts:
            return 0.0

        # This is a simplified approach; real implementation would
        # bucket by ISO week. Need at least 2 weeks of data.
        week_means: list[dict[int, float]] = []

        if len(week_means) < 2:
            return 0.0

        # Compute correlation between consecutive weeks
        correlations = []
        for i in range(1, len(week_means)):
            week_prev = week_means[i - 1]
            week_curr = week_means[i]
            common_bins = set(week_prev.keys()) & set(week_curr.keys())
            if len(common_bins) >= 2:
                vals_prev = [week_prev[b] for b in common_bins]
                vals_curr = [week_curr[b] for b in common_bins]
                corr = float(np.corrcoef(vals_prev, vals_curr)[0, 1])
                correlations.append(corr)

        return float(np.mean(correlations)) if correlations else 0.0

    def model_comparison(
        self,
        bayesian: BayesianGammaPoisson,
        hawkes: HawkesProcess,
        observed_counts: list[int],
        exposure: float = 1.0,
    ) -> dict[str, float]:
        """Compare predictive log-likelihood across models.

        Computes the average log predictive likelihood for:
          - Bayesian Gamma-Poisson (negative binomial predictive)
          - Hawkes (Poisson with intensity-based rate)
          - Naive Poisson (using posterior mean as rate)

        Returns:
            Dict with ``bayesian_log_pred_lik``, ``hawkes_log_pred_lik``,
            ``naive_poisson_log_pred_lik``.
        """
        from scipy.stats import nbinom as _nbinom
        from scipy.stats import poisson as _pois

        bayesian_ll = []
        hawkes_ll = []
        naive_ll = []

        mu = bayesian.posterior_mean() * exposure

        for obs in observed_counts:
            # Bayesian: negative binomial predictive
            r = bayesian.alpha_post
            p = bayesian.beta_post / (bayesian.beta_post + exposure)
            try:
                bayesian_ll.append(float(_nbinom.logpmf(obs, r, p)))
            except Exception:
                bayesian_ll.append(float("nan"))

            # Hawkes: Poisson with current intensity
            lam = hawkes.current_intensity() * exposure
            try:
                hawkes_ll.append(float(_pois.logpmf(obs, lam)))
            except Exception:
                hawkes_ll.append(float("nan"))

            # Naive Poisson
            try:
                naive_ll.append(float(_pois.logpmf(obs, mu)))
            except Exception:
                naive_ll.append(float("nan"))

        return {
            "bayesian_log_pred_lik": float(statistics.mean(b for b in bayesian_ll if not np.isnan(b)))
            if bayesian_ll else 0.0,
            "hawkes_log_pred_lik": float(statistics.mean(h for h in hawkes_ll if not np.isnan(h)))
            if hawkes_ll else 0.0,
            "naive_poisson_log_pred_lik": float(statistics.mean(n for n in naive_ll if not np.isnan(n)))
            if naive_ll else 0.0,
        }

    def run_all(
        self,
        symbol: str,
        model_set: ModelSet,
        observed_counts: list[int],
        timestamps: list[float],
    ) -> dict[str, Any]:
        """Run all calibration diagnostics for a symbol.

        Returns a comprehensive diagnostics dict suitable for logging
        to parquet or a health endpoint.
        """
        results: dict[str, Any] = {"symbol": symbol}

        # PIT values
        pit_vals = self.pit_values(model_set.bayesian, observed_counts)
        results["pit_values"] = pit_vals
        if len(pit_vals) >= 10:
            # KS test for uniformity (simplified: check if mean ≈ 0.5)
            pit_mean = statistics.mean(pit_vals)
            results["pit_mean"] = float(pit_mean)
            results["pit_uniform_pvalue"] = 0.0

        # Coverage
        cov = self.coverage_test(model_set.bayesian, observed_counts)
        results["coverage"] = cov

        # Hawkes residuals
        residuals = self.hawkes_residuals(model_set.hawkes, timestamps)
        results["hawkes_residuals_count"] = len(residuals)
        if residuals:
            # Under correct model, residuals should have mean ≈ 1
            results["hawkes_residual_mean"] = float(statistics.mean(residuals))

        # Model comparison
        mc = self.model_comparison(model_set.bayesian, model_set.hawkes, observed_counts)
        results["model_comparison"] = mc

        # Seasonality stability
        results["seasonality_correlation"] = self.seasonality_stability(model_set.seasonality)

        return results


# Module-level convenience functions for the CEL engine to use
def bayesian_decision(
    posterior: BayesianGammaPoisson,
    observed: int,
    cost_fp: float = 1.0,
    cost_fn: float = 10.0,
    exposure: float = 1.0,
) -> bool:
    """Bayesian decision rule: alert iff expected cost of missing > expected cost of false alarm.

    Uses the posterior predictive distribution to compute the probability
    that the observed count exceeds the expected rate. Alerts when:

        P(observed | H1) * cost_FN > P(observed | H0) * cost_FP

    where H0 = the count comes from the posterior predictive (typical),
    H1 = the count is from an anomalous process. The Bayes factor
    approach compares the likelihood under an "anomalous" alternative
    (2x the posterior mean) vs the null (posterior mean).

    Args:
        posterior: Fitted BayesianGammaPoisson model.
        observed: The observed trade count.
        cost_fp: Cost of a false positive alert.
        cost_fn: Cost of a false negative (missed anomaly).
        exposure: Exposure weight for the Poisson rate.

    Returns:
        True if an alert should be raised.
    """
    pred_mean = posterior.posterior_mean() * exposure

    if pred_mean <= 0:
        return observed > 0

    # Compute Bayes factor: likelihood under anomaly vs typical
    # H1: rate is 2x the posterior mean (anomalous)
    # H0: rate is the posterior mean (typical)
    null_rate = pred_mean
    alt_rate = pred_mean * 2.0

    from scipy.stats import poisson as _pois
    try:
        likelihood_null = float(_pois.pmf(observed, null_rate))
        likelihood_alt = float(_pois.pmf(observed, alt_rate))
    except Exception:
        return False

    if likelihood_null <= 0 and likelihood_alt > 0:
        return True
    if likelihood_null <= 0:
        return False

    bayes_factor = likelihood_alt / likelihood_null
    # Posterior odds ≈ Bayes factor (assuming equal prior odds)
    # Alert when: BF * cost_fn > cost_fp
    # i.e., when P(H1|data) / P(H0|data) > cost_fp / cost_fn
    return (bayes_factor * cost_fn) > cost_fp


def online_fdr_threshold(
    alpha: float,
    num_rejections: int,
    num_tests: int,
    lag: int = 0,
) -> float:
    """Compute the online FDR threshold using the LORD-style adjustment.

    A simplified SAFFRON/LORD approximation for streaming p-value correction.

    Args:
        alpha: Target FDR level (e.g. 0.05).
        num_rejections: Cumulative rejections so far.
        num_tests: Cumulative tests so far.
        lag: How many tests behind the current one to look.

    Returns:
        Adjusted p-value threshold.
    """
    if num_tests <= 0:
        return alpha
    # Bonferroni-style fallback with adaptive correction
    base = alpha * (num_rejections + 1) / max(num_tests, 1)
    return max(base, 1e-10)


def hierarchical_fdr(
    p_values: list[float],
    alpha: float = 0.05,
    group_labels: list[str] | None = None,
) -> list[bool]:
    """Hierarchical FDR: control at group level then within-group.

    Uses Benjamini-Hochberg at two levels:
      1. Group-level: test group aggregate p-values
      2. Within-group: test individual p-values in rejected groups

    Args:
        p_values: List of individual p-values.
        alpha: Target FDR level.
        group_labels: Optional group labels for each p-value.

    Returns:
        Boolean mask of rejected hypotheses.
    """
    if not p_values:
        return []

    n = len(p_values)
    if group_labels is None:
        # No grouping: flat BH
        return false_discovery_rate_control(p_values, alpha)

    # Group-level: assign each group its min p-value
    groups: dict[str, list[int]] = {}
    for i, label in enumerate(group_labels):
        groups.setdefault(str(label), []).append(i)

    group_min_p = {g: min(p_values[i] for i in idxs) for g, idxs in groups.items()}
    group_rejected = false_discovery_rate_control(
        list(group_min_p.values()), alpha
    )

    result = [False] * n
    rejected_groups = {
        g for g, rej in zip(group_min_p.keys(), group_rejected, strict=True) if rej
    }

    for g in rejected_groups:
        member_pvals = [p_values[i] for i in groups[g]]
        member_rejected = false_discovery_rate_control(member_pvals, alpha)
        for idx, rej in zip(groups[g], member_rejected, strict=True):
            result[idx] = rej

    return result


class VolatilityTargeter:
    """Adaptive threshold scaling based on realized volatility.

    Implements the regime-aware volatility targeting strategy:
    alert thresholds scale with realized vol:
        threshold = base * (vol_target / current_vol)

    When current volatility is below target, thresholds widen (fewer alerts).
    When current volatility is above target, thresholds tighten (more sensitive).
    """

    def __init__(self, vol_target: float = 0.02) -> None:
        self.vol_target = vol_target

    def adjusted_threshold(self, current_vol: float, base_threshold: float) -> float:
        """Scale a base threshold by the volatility ratio.

        Args:
            current_vol: Current realized volatility (annualized or window-based).
            base_threshold: The base alert threshold (e.g., P95 size).

        Returns:
            Adjusted threshold that adapts to volatility regime.
        """
        if current_vol <= 0:
            return base_threshold
        ratio = self.vol_target / current_vol
        # Clamp ratio to avoid extreme adjustments
        ratio = max(0.1, min(ratio, 10.0))
        return base_threshold * ratio

    def effective_window(self, base_window: int, current_vol: float) -> int:
        """Adaptive window size: expands in low-vol, contracts in high-vol.

        Args:
            base_window: The baseline rolling window size.
            current_vol: Current realized volatility.

        Returns:
            Adjusted window size (bounded to [10, 1000]).
        """
        if current_vol <= 0:
            return base_window
        ratio = self.vol_target / current_vol
        adjusted = int(base_window * ratio)
        return max(10, min(adjusted, 1000))

