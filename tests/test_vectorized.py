"""Tests for Sprint 8: Vectorized model updates and memory optimization."""

from __future__ import annotations

from dxlink_scanner.stats import BayesianGammaPoisson, HawkesProcess
from dxlink_scanner.stats.vectorized import (
    VectorizedBayesianUpdater,
    VectorizedHawkesUpdater,
)


class TestVectorizedBayesianUpdater:
    """Tests for batch Bayesian Gamma-Poisson updates."""

    def test_init_symbols(self) -> None:
        updater = VectorizedBayesianUpdater(symbols=["SPY", "QQQ", "ES"])
        assert updater.symbols == ["SPY", "QQQ", "ES"]
        assert len(updater.alpha_prior) == 3
        assert len(updater.beta_prior) == 3

    def test_batch_update(self) -> None:
        updater = VectorizedBayesianUpdater(symbols=["SPY", "QQQ"])
        counts = {"SPY": [1, 1, 1, 2], "QQQ": [1, 1]}
        updater.batch_update(counts)

        means = updater.get_posterior_means()
        # SPY: alpha_post = 1 + 5 = 6, beta_post = 1 + 4 = 5, mean = 6/5 = 1.2
        assert abs(means["SPY"] - 1.2) < 1e-10
        # QQQ: alpha_post = 1 + 2 = 3, beta_post = 1 + 2 = 3, mean = 1.0
        assert abs(means["QQQ"] - 1.0) < 1e-10

    def test_posterior_arrays(self) -> None:
        updater = VectorizedBayesianUpdater(symbols=["SPY"])
        updater.batch_update({"SPY": [1, 1, 1]})
        assert updater.alpha_post_array[0] == 4.0  # 1 + 3
        assert updater.beta_post_array[0] == 4.0   # 1 + 3

    def test_sync_to_models(self) -> None:
        updater = VectorizedBayesianUpdater(symbols=["SPY", "QQQ"])
        updater.batch_update({"SPY": [1, 1, 1], "QQQ": [1, 1]})

        models = {"SPY": BayesianGammaPoisson(), "QQQ": BayesianGammaPoisson()}
        updater.sync_to_models(models)

        assert models["SPY"].alpha_post == 4.0
        assert models["QQQ"].alpha_post == 3.0

    def test_credible_interval(self) -> None:
        updater = VectorizedBayesianUpdater(symbols=["SPY"])
        # alpha_prior=1, beta_prior=1, 10 updates of count=1
        # alpha_post = 1 + 10 = 11, beta_post = 1 + 10 = 11, mean = 1.0
        updater.batch_update({"SPY": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]})
        intervals = updater.credible_interval_batch(confidence=0.95)
        assert "SPY" in intervals
        low, high = intervals["SPY"]
        # Posterior mean = 1.0, 95% CI should bracket ~1.0
        assert low < 1.0
        assert high > 1.0

    def test_unknown_symbol_ignored(self) -> None:
        updater = VectorizedBayesianUpdater(symbols=["SPY"])
        updater.batch_update({"UNKNOWN": [1, 1, 1]})
        means = updater.get_posterior_means()
        assert means["SPY"] == 1.0  # unchanged from prior

    def test_prior_arrays_preserved(self) -> None:
        updater = VectorizedBayesianUpdater(symbols=["SPY"])
        updater.batch_update({"SPY": [1, 1]})
        assert updater.alpha_prior[0] == 1.0  # prior unchanged
        assert updater.alpha_post[0] == 3.0  # 1 + 2


class TestVectorizedHawkesUpdater:
    """Tests for batch Hawkes process intensity calculations."""

    def test_init(self) -> None:
        updater = VectorizedHawkesUpdater(symbols=["SPY", "QQQ"])
        assert updater.symbols == ["SPY", "QQQ"]
        assert updater.mu == 0.1

    def test_add_single_event(self) -> None:
        updater = VectorizedHawkesUpdater(symbols=["SPY"])
        updater.add_event("SPY", 1000.0)
        intensities = updater.compute_intensities(1001.0)
        assert "SPY" in intensities
        assert intensities["SPY"] > updater.mu  # Should have excitation

    def test_add_multiple_events(self) -> None:
        updater = VectorizedHawkesUpdater(symbols=["SPY"])
        updater.add_events("SPY", [1000.0, 1000.5, 1001.0])
        intensities = updater.compute_intensities(1002.0)
        assert intensities["SPY"] > updater.mu

    def test_no_events(self) -> None:
        updater = VectorizedHawkesUpdater(symbols=["SPY"])
        intensities = updater.compute_intensities(1000.0)
        assert intensities["SPY"] == updater.mu

    def test_intensity_batch(self) -> None:
        updater = VectorizedHawkesUpdater(symbols=["SPY", "QQQ"])
        updater.add_event("SPY", 1000.0)
        updater.add_event("QQQ", 1000.0)
        updater.add_event("SPY", 1000.5)
        arr = updater.compute_intensities_batch(1001.0)
        assert len(arr) == 2
        assert arr[0] > updater.mu  # SPY has 2 events
        assert arr[1] > updater.mu  # QQQ has 1 event
        assert arr[0] > arr[1]      # SPY has more excitation

    def test_sync_to_models(self) -> None:
        updater = VectorizedHawkesUpdater(symbols=["SPY"])
        updater.add_event("SPY", 1000.0)
        updater.add_event("SPY", 1001.0)
        updater.compute_intensities(1002.0)

        model = HawkesProcess()
        updater.sync_to_models({"SPY": model})
        assert model._current_intensity > model.mu

    def test_unknown_symbol_ignored(self) -> None:
        updater = VectorizedHawkesUpdater(symbols=["SPY"])
        updater.add_event("UNKNOWN", 1000.0)
        updater.add_events("UNKNOWN", [1000.0, 1001.0])
        intensities = updater.compute_intensities(1002.0)
        assert intensities["SPY"] == updater.mu

    def test_exponential_decay(self) -> None:
        """Intensity should decrease as events age."""
        updater = VectorizedHawkesUpdater(symbols=["SPY"], mu=0.1, alpha=1.0, beta=2.0)
        updater.add_events("SPY", [1000.0, 1000.1, 1000.2])

        early = updater.compute_intensities(1000.3)
        late = updater.compute_intensities(1005.0)  # events are much older

        assert early["SPY"] > late["SPY"]
        # Late intensity should be close to baseline
        assert abs(late["SPY"] - updater.mu) < 0.1
