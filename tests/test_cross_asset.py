"""Tests for Sprint 6: Cross-asset & multi-timeframe analysis."""

from __future__ import annotations

import pytest

from dxlink_scanner.stats import CrossAssetFlowState, CrossAssetHawkes, compute_lead_lag


class TestCrossAssetHawkes:
    """Tests for multivariate Hawkes process."""

    def test_initial_state(self) -> None:
        hk = CrossAssetHawkes(symbols=["SPY", "QQQ", "ES"], mu=0.1, decay=1.0)
        assert hk.symbols == ["SPY", "QQQ", "ES"]
        assert hk._intensities == {"SPY": 0.1, "QQQ": 0.1, "ES": 0.1}

    def test_add_event_updates_intensities(self) -> None:
        hk = CrossAssetHawkes(symbols=["SPY", "QQQ"], mu=0.1, decay=1.0)
        hk.add_event("SPY", 1.0)
        # SPY should have higher intensity than QQQ (self-excitation)
        assert hk._intensities["SPY"] > 0.1
        assert hk._intensities["QQQ"] > 0.1  # Cross-excitation too

    def test_expected_events(self) -> None:
        hk = CrossAssetHawkes(symbols=["SPY", "QQQ"], mu=0.1, decay=1.0)
        hk.add_event("SPY", 1.0)
        expected = hk.expected_events("SPY", horizon=60.0)
        assert expected > 0.0
        # With elevated intensity, expected should be > mu * horizon
        assert expected > 0.1 * 60.0

    def test_systemic_anomaly_score_no_events(self) -> None:
        hk = CrossAssetHawkes(symbols=["SPY", "QQQ"], mu=0.1, decay=1.0)
        # Initially, intensity ≈ mu, so score ≈ 1.0
        score = hk.systemic_anomaly_score()
        assert score == pytest.approx(1.0, rel=0.1)

    def test_systemic_anomaly_score_elevated(self) -> None:
        hk = CrossAssetHawkes(symbols=["SPY", "QQQ", "ES"], mu=0.1, decay=1.0)
        # Add many events across symbols
        for t in range(100):
            hk.add_event("SPY", float(t))
            hk.add_event("QQQ", float(t))
            hk.add_event("ES", float(t))
        score = hk.systemic_anomaly_score()
        assert score > 1.0  # Elevated above baseline

    def test_serialization_roundtrip(self) -> None:
        hk = CrossAssetHawkes(symbols=["SPY", "QQQ"], mu=0.1, decay=0.5)
        hk.add_event("SPY", 1.0)
        hk.add_event("QQQ", 2.0)
        data = hk.to_dict()
        hk2 = CrossAssetHawkes.from_dict(data)
        assert hk2.symbols == hk.symbols
        assert hk2._intensities == hk._intensities
        assert hk2._last_events == hk._last_events

    def test_alpha_initialization(self) -> None:
        hk = CrossAssetHawkes(symbols=["SPY", "QQQ"], mu=0.1)
        # All-to-all excitation by default
        for s in hk.symbols:
            for t in hk.symbols:
                assert hk.alpha[s][t] == 0.5


class TestCrossAssetFlowState:
    """Tests for per-symbol flow state tracking."""

    def test_add_trade(self) -> None:
        state = CrossAssetFlowState(symbol="SPY")
        state.add_trade(100, 1.0)
        state.add_trade(200, 2.0)
        assert len(state.volumes) == 2
        assert state.volumes[-1] == 200
        assert state.timestamps[-1] == 2.0

    def test_recent_volume(self) -> None:
        state = CrossAssetFlowState(symbol="QQQ")
        assert state.recent_volume == 0.0
        state.add_trade(100, 1.0)
        state.add_trade(200, 2.0)
        assert state.recent_volume == 300

    def test_maxlen_respected(self) -> None:
        state = CrossAssetFlowState(symbol="ES")
        for i in range(2000):
            state.add_trade(i, float(i))
        assert len(state.volumes) <= 1000


class TestComputeLeadLag:
    """Tests for lead-lag cross-correlation analysis."""

    def test_positive_correlation(self) -> None:
        """When A leads B, correlation should be positive at the correct lag."""
        from dxlink_scanner.stats import CrossAssetFlowState

        fa = CrossAssetFlowState(symbol="A")
        fb = CrossAssetFlowState(symbol="B")
        # A leads B: A has varying volume, B mirrors at lag 1
        for i in range(20):
            vol = 100 + (i % 3) * 50  # alternating 100, 150, 200
            fa.add_trade(vol, float(i))
            fb.add_trade(vol, float(i + 1))
        result = compute_lead_lag("A", "B", fa, fb, lag_steps=5)
        assert result["symbol_a"] == "A"
        assert result["symbol_b"] == "B"
        assert result["best_lag"] >= 1
        assert result["lead_sign"] == "positive"

    def test_insufficient_data(self) -> None:
        fa = CrossAssetFlowState(symbol="A")
        fb = CrossAssetFlowState(symbol="B")
        result = compute_lead_lag("A", "B", fa, fb, lag_steps=5)
        assert result["lead_sign"] == "insufficient_data"
        assert result["best_lag"] == 0

    def test_negative_correlation(self) -> None:
        """When A and B move oppositely, correlation should be negative."""
        fa = CrossAssetFlowState(symbol="A")
        fb = CrossAssetFlowState(symbol="B")
        for i in range(20):
            vol_a = 100 if i % 2 == 0 else 50
            vol_b = 50 if i % 2 == 0 else 100
            fa.add_trade(vol_a, float(i))
            fb.add_trade(vol_b, float(i + 1))
        result = compute_lead_lag("A", "B", fa, fb, lag_steps=5)
        assert result["lead_sign"] == "negative"
