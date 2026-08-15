"""Tests for model persistence, warm-up, and calibration diagnostics."""

from __future__ import annotations

import datetime as dt
import tempfile

import pytest

from dxlink_scanner.stats import (
    BayesianGammaPoisson,
    CalibrationDiagnostics,
    CrossSymbolPool,
    HawkesProcess,
    ModelSet,
    ModelStore,
    RegimeDetector,
    TimeOfDaySeasonality,
    VolatilityTargeter,
    VolumeAtPrice,
    bayesian_decision,
    hierarchical_fdr,
    online_fdr_threshold,
    prior_elicitation,
)


class TestModelSerialization:
    """Test to_dict/from_dict round-trip for all 6 models."""

    def test_bayesian_round_trip(self) -> None:
        model = BayesianGammaPoisson(alpha=2.0, beta=3.0)
        model.update(5)
        model.update(10)
        data = model.to_dict()
        restored = BayesianGammaPoisson.from_dict(data)
        assert restored.alpha == 2.0
        assert restored.beta == 3.0
        assert restored.alpha_post == model.alpha_post
        assert restored.beta_post == model.beta_post
        assert restored.n_observations == 2
        assert restored.sum_counts == 15
        # Functional equivalence
        assert restored.posterior_mean() == model.posterior_mean()
        ci = restored.credible_interval(0.95)
        assert ci[0] == pytest.approx(model.credible_interval(0.95)[0])

    def test_hawkes_round_trip(self) -> None:
        hp = HawkesProcess(mu=0.1, alpha=0.5, beta=1.0)
        hp.add_event(1000.0)
        hp.add_event(1001.0)
        data = hp.to_dict()
        restored = HawkesProcess.from_dict(data)
        assert restored.mu == 0.1
        assert restored.alpha == 0.5
        assert restored.beta == 1.0
        assert restored._event_times == [1000.0, 1001.0]
        assert restored._current_intensity == hp._current_intensity

    def test_seasonality_round_trip(self) -> None:
        tod = TimeOfDaySeasonality(bin_edges=[570, 600, 630])
        tod.add_observation(dt.datetime(2024, 1, 15, 13, 30, tzinfo=dt.UTC), 100.0)
        tod.add_observation(dt.datetime(2024, 1, 15, 14, 0, tzinfo=dt.UTC), 200.0)
        data = tod.to_dict()
        restored = TimeOfDaySeasonality.from_dict(data)
        assert restored.bin_edges == [570, 600, 630]
        assert restored._bin_means == tod._bin_means
        assert restored._global_mean == tod._global_mean

    def test_cross_symbol_pool_round_trip(self) -> None:
        pool = CrossSymbolPool(global_alpha=5.0, global_beta=2.0)
        pool.update_symbol("SPY", 10)
        pool.update_symbol("QQQ", 20)
        data = pool.to_dict()
        restored = CrossSymbolPool.from_dict(data)
        assert restored.global_alpha == 5.0
        assert restored.global_beta == 2.0
        assert "SPY" in restored.symbol_posteriors
        assert "QQQ" in restored.symbol_posteriors
        assert restored_mean(restored, "SPY") == pytest.approx(
            pool.symbol_posteriors["SPY"].posterior_mean()
        )

    def test_volume_at_price_round_trip(self) -> None:
        vap = VolumeAtPrice(tick_size=0.01)
        vap.add_trade(100.00, 50)
        vap.add_trade(100.01, 100)
        data = vap.to_dict()
        restored = VolumeAtPrice.from_dict(data)
        assert restored.tick_size == 0.01
        assert restored._price_volume == vap._price_volume
        assert restored._total_volume == vap._total_volume
        assert restored.poc() == vap.poc()

    def test_regime_detector_round_trip(self) -> None:
        rd = RegimeDetector(vol_low=0.01, vol_high=0.03, vol_crash=0.05, vol_window=50)
        rd.update(100.0, 100)
        rd.update(101.0, 200)
        rd.update(99.5, 150)
        data = rd.to_dict()
        restored = RegimeDetector.from_dict(data)
        assert restored.vol_low == 0.01
        assert restored.vol_high == 0.03
        assert restored.vol_window == 50
        assert restored._returns == rd._returns
        assert restored._volumes == rd._volumes


def restored_mean(pool: CrossSymbolPool, symbol: str) -> float:
    return pool.symbol_posteriors[symbol].posterior_mean()


class TestModelSet:
    """Test ModelSet container serialization."""

    def test_model_set_round_trip(self) -> None:
        ms = ModelSet()
        ms.bayesian.update(10)
        ms.hawkes.add_event(1000.0)
        ms.hawkes.add_event(1005.0)
        data = ms.to_dict()
        restored = ModelSet.from_dict(data)
        assert restored.bayesian.n_observations == 1
        assert restored.bayesian.sum_counts == 10
        assert restored.hawkes._event_times == [1000.0, 1005.0]
        assert restored.hawkes.mu == ms.hawkes.mu


class TestModelStore:
    """Test ModelStore save/load/warm_up/checkpoint."""

    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelStore(data_dir=tmpdir)
            models: dict[str, ModelSet] = {}
            ms = ModelSet()
            ms.bayesian.update(5)
            ms.bayesian.update(10)
            ms.hawkes.add_event(1000.0)
            models["SPY"] = ms

            store.save(models)
            assert store.models_path.exists()

            loaded = store.load()
            assert "SPY" in loaded
            assert loaded["SPY"].bayesian.n_observations == 2
            assert loaded["SPY"].bayesian.sum_counts == 15
            assert loaded["SPY"].hawkes._event_times == [1000.0]

    def test_load_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelStore(data_dir=tmpdir)
            loaded = store.load()
            assert loaded == {}

    def test_warm_up_from_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelStore(data_dir=tmpdir)
            models = store.warm_up(["SPY", "QQQ", "default"])
            assert "SPY" in models
            assert "QQQ" in models
            assert "default" in models
            # Default priors
            assert models["SPY"].bayesian.alpha_post == 1.0
            assert models["SPY"].bayesian.beta_post == 1.0

    def test_warm_up_from_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelStore(data_dir=tmpdir)
            # Save first
            models: dict[str, ModelSet] = {}
            ms = ModelSet()
            ms.bayesian.update(50)
            models["SPY"] = ms
            store.save(models)

            # Warm up should load saved state
            warmed = store.warm_up(["SPY", "default"])
            assert warmed["SPY"].bayesian.n_observations == 1
            assert warmed["SPY"].bayesian.sum_counts == 50
            assert warmed["SPY"].bayesian.alpha_post == 51.0  # 1 + 50

    def test_maybe_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelStore(data_dir=tmpdir, checkpoint_interval_sec=0.1)
            models: dict[str, ModelSet] = {"SPY": ModelSet()}
            models["SPY"].bayesian.update(5)
            # Should not checkpoint immediately
            store.maybe_checkpoint(models)
            assert not store.models_path.exists()

            # Force checkpoint by backdating
            store._last_checkpoint = 0.0
            store.maybe_checkpoint(models)
            assert store.models_path.exists()


class TestPriorElicitation:
    """Test empirical Bayes prior estimation from parquet data."""

    def test_no_parquet_dir(self) -> None:
        """Should return defaults when parquet directory doesn't exist."""
        result = prior_elicitation("/nonexistent/path/to/parquet", lookback_days=30)
        assert "alpha" in result
        assert result["alpha"] == 1.0
        assert result["beta"] == 1.0


class TestCalibrationDiagnostics:
    """Tests for calibration and validation diagnostics."""

    def test_pit_values(self) -> None:
        model = BayesianGammaPoisson(alpha=10.0, beta=10.0)
        model.update(20)
        diag = CalibrationDiagnostics()
        observed = [15, 20, 25, 10, 30]
        pit = diag.pit_values(model, observed)
        assert len(pit) == 5
        for v in pit:
            assert 0.0 <= v <= 1.0

    def test_pit_values_empty(self) -> None:
        model = BayesianGammaPoisson()
        diag = CalibrationDiagnostics()
        pit = diag.pit_values(model, [])
        assert pit == []

    def test_coverage_test(self) -> None:
        model = BayesianGammaPoisson(alpha=10.0, beta=10.0)
        model.update(20)
        diag = CalibrationDiagnostics()
        # Use observed values near the posterior mean (~2) so most fall within CI
        observed = [2, 3, 1, 2, 3] * 10
        result = diag.coverage_test(model, observed)
        assert "coverage_rate" in result
        assert 0.0 <= result["coverage_rate"] <= 1.0
        assert result["n_observations"] == 50
        # Most observations within a Poisson CI around mean~2 should have high coverage
        assert result["coverage_rate"] > 0.5

    def test_coverage_test_empty(self) -> None:
        model = BayesianGammaPoisson()
        diag = CalibrationDiagnostics()
        result = diag.coverage_test(model, [])
        assert result["coverage_rate"] == 0.0
        assert result["n_observations"] == 0

    def test_hawkes_residuals(self) -> None:
        hp = HawkesProcess(mu=0.1, alpha=0.5, beta=1.0)
        timestamps = [float(i) for i in range(10)]
        diag = CalibrationDiagnostics()
        residuals = diag.hawkes_residuals(hp, timestamps)
        assert len(residuals) == 9  # n-1 residuals for n events
        for r in residuals:
            assert r >= 0.0

    def test_hawkes_residuals_too_few(self) -> None:
        hp = HawkesProcess()
        diag = CalibrationDiagnostics()
        residuals = diag.hawkes_residuals(hp, [1000.0])
        assert residuals == []

    def test_model_comparison(self) -> None:
        bayesian = BayesianGammaPoisson(alpha=10.0, beta=10.0)
        bayesian.update(20)
        hawkes = HawkesProcess(mu=0.1, alpha=0.5, beta=1.0)
        hawkes.add_event(1000.0)
        diag = CalibrationDiagnostics()
        observed = [15, 20, 25, 30]
        result = diag.model_comparison(bayesian, hawkes, observed)
        assert "bayesian_log_pred_lik" in result
        assert "hawkes_log_pred_lik" in result
        assert "naive_poisson_log_pred_lik" in result

    def test_run_all(self) -> None:
        ms = ModelSet()
        ms.bayesian.update(10)
        ms.bayesian.update(20)
        ms.hawkes.add_event(1000.0)
        ms.hawkes.add_event(1001.0)
        diag = CalibrationDiagnostics()
        result = diag.run_all("SPY", ms, [10, 20, 30], [1000.0, 1001.0])
        assert result["symbol"] == "SPY"
        assert "pit_values" in result
        assert "coverage" in result
        assert "hawkes_residuals_count" in result
        assert "model_comparison" in result
        assert "seasonality_correlation" in result


class TestDecisionFunctions:
    """Tests for the new decision-theoretic CEL helper functions."""

    def test_bayesian_decision_anomaly(self) -> None:
        # With a moderate posterior mean and a clearly anomalous observation
        posterior = BayesianGammaPoisson(alpha=50.0, beta=10.0)  # mean=5
        posterior.update(50)  # mean stays ~5, observed=5 is typical
        # An observation of 50 (10x the mean) should be very anomalous
        result = bayesian_decision(posterior, 50, cost_fp=1.0, cost_fn=10.0)
        assert result is True

    def test_bayesian_decision_no_anomaly(self) -> None:
        posterior = BayesianGammaPoisson(alpha=50.0, beta=10.0)  # mean=5
        posterior.update(50)
        # An observation near the mean should not trigger alert
        result = bayesian_decision(posterior, 5, cost_fp=1.0, cost_fn=10.0)
        assert result is False

    def test_online_fdr_threshold(self) -> None:
        threshold = online_fdr_threshold(alpha=0.05, num_rejections=5, num_tests=100)
        assert threshold > 0
        assert threshold <= 0.05  # should be conservative

    def test_online_fdr_threshold_zero_tests(self) -> None:
        threshold = online_fdr_threshold(alpha=0.05, num_rejections=0, num_tests=0)
        assert threshold == 0.05  # fallback to alpha

    def test_hierarchical_fdr_flat(self) -> None:
        pvals = [0.001, 0.01, 0.5, 0.6]
        result = hierarchical_fdr(pvals, alpha=0.05)
        assert len(result) == 4
        assert result[0] is True
        assert result[1] is True

    def test_hierarchical_fdr_grouped(self) -> None:
        pvals = [0.001, 0.01, 0.02, 0.5, 0.6]
        groups = ["A", "A", "A", "B", "B"]
        result = hierarchical_fdr(pvals, alpha=0.05, group_labels=groups)
        assert len(result) == 5
        # Group A has significant p-values, should reject
        assert result[0] is True

    def test_hierarchical_fdr_empty(self) -> None:
        result = hierarchical_fdr([], alpha=0.05)
        assert result == []


class TestVolatilityTargeter:
    """Tests for the VolatilityTargeter class."""

    def test_adjusted_threshold_low_vol(self) -> None:
        vt = VolatilityTargeter(vol_target=0.02)
        # Current vol below target → threshold should be higher
        result = vt.adjusted_threshold(current_vol=0.01, base_threshold=100.0)
        assert result > 100.0

    def test_adjusted_threshold_high_vol(self) -> None:
        vt = VolatilityTargeter(vol_target=0.02)
        # Current vol above target → threshold should be lower
        result = vt.adjusted_threshold(current_vol=0.04, base_threshold=100.0)
        assert result < 100.0

    def test_adjusted_threshold_at_target(self) -> None:
        vt = VolatilityTargeter(vol_target=0.02)
        result = vt.adjusted_threshold(current_vol=0.02, base_threshold=100.0)
        assert result == pytest.approx(100.0)

    def test_adjusted_threshold_zero_vol(self) -> None:
        vt = VolatilityTargeter(vol_target=0.02)
        result = vt.adjusted_threshold(current_vol=0.0, base_threshold=100.0)
        assert result == 100.0  # fallback to base

    def test_effective_window_low_vol(self) -> None:
        vt = VolatilityTargeter(vol_target=0.02)
        # Low vol → wider window
        result = vt.effective_window(base_window=50, current_vol=0.01)
        assert result > 50

    def test_effective_window_high_vol(self) -> None:
        vt = VolatilityTargeter(vol_target=0.02)
        # High vol → narrower window
        result = vt.effective_window(base_window=50, current_vol=0.04)
        assert result < 50

    def test_effective_window_bounds(self) -> None:
        vt = VolatilityTargeter(vol_target=0.02)
        result = vt.effective_window(base_window=50, current_vol=0.0001)
        assert result <= 1000  # clamped to max
        result = vt.effective_window(base_window=50, current_vol=100.0)
        assert result >= 10  # clamped to min


class TestRegimeDetector:
    """Tests for regime detection and serialization."""

    def test_detect_low_vol(self) -> None:
        rd = RegimeDetector(vol_low=0.01, vol_high=0.03, vol_crash=0.05)
        for i in range(15):
            rd.update(100.0 + i * 0.001, 100)  # very low vol
        state = rd.detect()
        assert 0 <= state.regime <= 3
        assert 0.0 <= state.probability <= 1.0

    def test_detect_high_vol(self) -> None:
        rd = RegimeDetector(vol_low=0.01, vol_high=0.03, vol_crash=0.05)
        for i in range(15):
            rd.update(100.0 + i * 5.0, 100)  # high vol
        state = rd.detect()
        assert state.regime == 3  # crash
        assert state.probability > 0.5

    def test_regime_detector_round_trip(self) -> None:
        rd = RegimeDetector(vol_low=0.01, vol_high=0.03, vol_crash=0.05, vol_window=50)
        for i in range(15):
            rd.update(100.0 + i * 0.1, 100)
        data = rd.to_dict()
        restored = RegimeDetector.from_dict(data)
        assert restored.vol_low == 0.01
        assert restored.vol_high == 0.03
        assert restored.vol_crash == 0.05
        assert restored.vol_window == 50
        assert restored._returns == rd._returns
        assert restored._volumes == rd._volumes
        # Functional equivalence
        state_orig = rd.detect()
        state_restored = restored.detect()
        assert state_orig.regime == state_restored.regime


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
