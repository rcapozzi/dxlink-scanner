"""Model health endpoint for monitoring scanner model quality in production.

Provides a `/health/models` endpoint (or CLI command) that returns:
- Calibration PIT values per symbol
- Coverage of 95% credible intervals
- Last parameter update timestamp
- Parameter drift from prior

Classes:
    ModelHealthSnapshot — Per-symbol health report
    ModelHealthMonitor — Aggregates health across all symbols
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from dxlink_scanner.stats import CalibrationDiagnostics, ModelSet

if TYPE_CHECKING:
    from dxlink_scanner.stats import RegimeDetector

logger = logging.getLogger(__name__)


@dataclass
class ModelHealthSnapshot:
    """Health snapshot for a single model set."""

    symbol: str
    last_update: dt.datetime | None = None
    n_observations: int = 0
    posterior_mean: float | None = None
    posterior_ci_low: float | None = None
    posterior_ci_high: float | None = None
    hawkes_intensity: float | None = None
    hawkes_mu: float | None = None
    regime: int | None = None
    regime_probability: float | None = None
    regime_volatility: float | None = None
    # Calibration metrics (updated from last batch)
    pit_uniformity_pvalue: float | None = None
    coverage_rate: float | None = None
    coverage_target: float = 0.95
    calibration_status: str = "unknown"  # "good", "warning", "critical", "unknown"
    # Drift detection
    alpha_drift: float | None = None  # |alpha_post - alpha_prior|
    beta_drift: float | None = None
    mu_drift: float | None = None
    # Overall health score 0-1
    health_score: float = 1.0


class ModelHealthMonitor:
    """Monitors model health across all symbols.

    Aggregates calibration diagnostics, parameter drift, and regime
    state into a single health snapshot suitable for HTTP endpoint
    or CLI command output.

    Attributes:
        model_sets: Dict of symbol → ModelSet to monitor.
        regime_detectors: Optional dict of symbol → RegimeDetector.
        coverage_target: Target coverage rate for CI validation (default: 0.95).
        coverage_tolerance: Acceptable deviation from target (default: 0.05).
    """

    def __init__(
        self,
        model_sets: dict[str, ModelSet],
        regime_detectors: dict[str, RegimeDetector] | None = None,
        coverage_target: float = 0.95,
        coverage_tolerance: float = 0.05,
    ) -> None:
        self.model_sets = model_sets
        self.regime_detectors = regime_detectors or {}
        self.coverage_target = coverage_target
        self.coverage_tolerance = coverage_tolerance
        self._diag = CalibrationDiagnostics()
        self._last_check: dt.datetime | None = None

    def check_all(self, recent_observations: dict[str, list[float]] | None = None) -> list[ModelHealthSnapshot]:
        """Run health checks for all model sets.

        Args:
            recent_observations: Optional recent observed values per symbol
                for calibration checking. If None, only static model state
                is reported.

        Returns:
            List of ModelHealthSnapshot, one per symbol.
        """
        snapshots: list[ModelHealthSnapshot] = []
        now = dt.datetime.now(dt.UTC)

        for symbol, ms in self.model_sets.items():
            snap = self._check_symbol(symbol, ms, recent_observations, now)
            snapshots.append(snap)

        self._last_check = now
        return snapshots

    def _check_symbol(
        self,
        symbol: str,
        ms: ModelSet,
        recent_observations: dict[str, list[float]] | None,
        now: dt.datetime,
    ) -> ModelHealthSnapshot:
        """Run health check for a single symbol's model set."""
        snap = ModelHealthSnapshot(
            symbol=symbol,
            last_update=now,
            n_observations=ms.bayesian.n_observations,
            posterior_mean=ms.bayesian.posterior_mean(),
        )

        # Bayesian credible interval
        try:
            ci_low, ci_high = ms.bayesian.credible_interval(0.95)
            snap.posterior_ci_low = ci_low
            snap.posterior_ci_high = ci_high
        except Exception:
            pass

        # Hawkes state
        snap.hawkes_intensity = ms.hawkes._current_intensity
        snap.hawkes_mu = ms.hawkes.mu

        # Regime state
        rd = self.regime_detectors.get(symbol)
        if rd is not None:
            try:
                state = rd.detect()
                snap.regime = state.regime
                snap.regime_probability = state.probability
                snap.regime_volatility = state.volatility
            except Exception:
                pass

        # Parameter drift from prior
        snap.alpha_drift = abs(ms.bayesian.alpha_post - ms.bayesian.alpha)
        snap.beta_drift = abs(ms.bayesian.beta_post - ms.bayesian.beta)
        if ms.hawkes.mu is not None:
            snap.mu_drift = abs(ms.hawkes._current_intensity - ms.hawkes.mu)

        # Calibration check if we have recent observations
        if recent_observations and symbol in recent_observations:
            obs = recent_observations[symbol]
            if obs:
                try:
                    obs_int = [int(v) for v in obs]
                    coverage = self._diag.coverage_test(ms.bayesian, obs_int)
                    snap.coverage_rate = coverage.get("coverage_rate")
                    snap.coverage_target = coverage.get("target_coverage", self.coverage_target)
                    if snap.coverage_rate is not None:
                        deviation = abs(snap.coverage_rate - self.coverage_target)
                        if deviation > self.coverage_tolerance * 2:
                            snap.calibration_status = "critical"
                            snap.health_score = max(0.0, snap.health_score - 0.5)
                        elif deviation > self.coverage_tolerance:
                            snap.calibration_status = "warning"
                            snap.health_score = max(0.0, snap.health_score - 0.3)
                        else:
                            snap.calibration_status = "good"
                except Exception as e:
                    logger.debug("Coverage check failed for %s: %s", symbol, e)

                # PIT uniformity
                try:
                    obs_int = [int(v) for v in obs]  # pit_values also expects int counts
                    pit = self._diag.pit_values(ms.bayesian, obs_int)
                    if pit:
                        # KS-like test: if PIT values are roughly uniform [0,1]
                        # check if they deviate from uniform
                        n = len(pit)

                        def expected_cdf(x: float) -> float:
                            return x  # uniform CDF

                        pit_sorted = sorted(pit)
                        ks_stat = max(abs((i + 1) / n - expected_cdf(v)) for i, v in enumerate(pit_sorted))
                        # Rough KS threshold for n>10
                        if n > 10:
                            ks_threshold = 1.36 / (n**0.5)  # 95% KS critical value
                            snap.pit_uniformity_pvalue = 1.0 - ks_stat / ks_threshold
                        else:
                            snap.pit_uniformity_pvalue = None
                except Exception as e:
                    logger.debug("PIT check failed for %s: %s", symbol, e)

        # Health score adjustment for drift
        if snap.alpha_drift is not None and snap.alpha_drift > 10:
            snap.health_score = max(0.0, snap.health_score - 0.2)
        if snap.beta_drift is not None and snap.beta_drift > 10:
            snap.health_score = max(0.0, snap.health_score - 0.2)

        return snap

    def to_json(self, snapshots: list[ModelHealthSnapshot]) -> str:
        """Serialize health snapshots to JSON."""
        return json.dumps([asdict(s) for s in snapshots], indent=2, default=str)

    def to_dict(self, snapshots: list[ModelHealthSnapshot]) -> dict:
        """Serialize health snapshots to a dict suitable for HTTP response."""
        return {
            "checked_at": self._last_check.isoformat() if self._last_check else None,
            "coverage_target": self.coverage_target,
            "symbols_checked": len(snapshots),
            "health": [asdict(s) for s in snapshots],
        }
