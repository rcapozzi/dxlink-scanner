"""Tests for Sprint 5: Microstructure analytics (VAP, VPIN, order flow classification, liquidity)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from dxlink_scanner.models import ConsolidatedSnapshot
from dxlink_scanner.stats import (
    FlowMetrics,
    LiquidityMetrics,
    OrderFlowClassifier,
    VPINCalculator,
)


class TestOrderFlowClassifier:
    """Tests for Lee-Ready / EMO trade classification."""

    def test_trade_at_ask_classified_buy(self) -> None:
        clf = OrderFlowClassifier()
        result = clf.classify(101.0, bid_price=100.0, ask_price=101.0, prev_mid=None, tick_direction=None)
        assert result.side == "buy"
        assert result.is_informed is True
        assert result.confidence > 0.8

    def test_trade_at_bid_classified_sell(self) -> None:
        clf = OrderFlowClassifier()
        result = clf.classify(100.0, bid_price=100.0, ask_price=101.0, prev_mid=None, tick_direction=None)
        assert result.side == "sell"
        assert result.is_informed is True

    def test_trade_in_spread_uses_tick_rule(self) -> None:
        clf = OrderFlowClassifier()
        result = clf.classify(100.5, bid_price=100.0, ask_price=101.0, prev_mid=100.25, tick_direction=1)
        assert result.side == "buy"
        assert result.is_informed is False

    def test_trade_in_spread_uses_emo(self) -> None:
        clf = OrderFlowClassifier()
        result = clf.classify(100.3, bid_price=100.0, ask_price=101.0, prev_mid=100.2, tick_direction=None)
        assert result.side == "buy"

    def test_no_bid_ask_uses_tick_direction(self) -> None:
        clf = OrderFlowClassifier()
        result = clf.classify(100.0, bid_price=None, ask_price=None, prev_mid=None, tick_direction=-1)
        assert result.side == "sell"
        assert result.is_informed is False

    def test_no_data_unknown(self) -> None:
        clf = OrderFlowClassifier()
        result = clf.classify(100.0, bid_price=None, ask_price=None, prev_mid=None, tick_direction=0)
        assert result.side == "unknown"
        assert result.confidence == 0.0


class TestVPINCalculator:
    """Tests for Volume-synchronized Probability of Informed Trading."""

    def test_initial_vpin_is_neutral(self) -> None:
        calc = VPINCalculator(bucket_volume=10, window_buckets=5)
        assert calc.vpin == 0.5  # Neutral when no data

    def test_balanced_buy_sell_flow(self) -> None:
        calc = VPINCalculator(bucket_volume=10, window_buckets=5)
        # Mixed buy/sell trades in each bucket — VPIN should be moderate
        for _ in range(5):
            calc.add_trade(100.5, 5, bid_price=100.0, ask_price=100.5)  # half buy
            calc.add_trade(99.5, 5, bid_price=100.0, ask_price=100.5)  # half sell
        assert calc.vpin < 0.99  # Should be balanced (many mixed buckets)

    def test_skewed_buy_flow(self) -> None:
        calc = VPINCalculator(bucket_volume=10, window_buckets=5)
        for _ in range(10):
            calc.add_trade(100.5, 10, bid_price=100.0, ask_price=100.5)
        assert calc.vpin > 0.5

    def test_bucket_completion(self) -> None:
        calc = VPINCalculator(bucket_volume=10, window_buckets=3)
        calc.add_trade(100.5, 5, bid_price=100.0, ask_price=100.5)
        calc.add_trade(100.5, 5, bid_price=100.0, ask_price=100.5)
        assert calc.vpin == 1.0

    def test_vpin_std(self) -> None:
        calc = VPINCalculator(bucket_volume=10, window_buckets=10)
        assert calc.vpin_std == 0.0
        # Create buckets with varying imbalance:
        # Even iterations: 10 buy, 0 sell → VPIN = 1.0
        # Odd iterations: 5 buy, 5 sell → VPIN = 0.0
        for i in range(10):
            if i % 2 == 0:
                for _ in range(10):
                    calc.add_trade(100.5, 1, bid_price=100.0, ask_price=100.5)
            else:
                for _ in range(5):
                    calc.add_trade(100.5, 1, bid_price=100.0, ask_price=100.5)
                for _ in range(5):
                    calc.add_trade(99.5, 1, bid_price=100.0, ask_price=100.5)
        vpin = calc.vpin
        std = calc.vpin_std
        # VPIN should be between 0 and 1, with std > 0
        assert 0.0 <= vpin <= 1.0
        assert std > 0.0


class TestLiquidityMetrics:
    """Tests for liquidity metrics (spread, depth, persistence)."""

    def test_spread_p50_and_p95(self) -> None:
        lm = LiquidityMetrics(window=100)
        for bps in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            lm.add_spread(float(bps))
        assert lm.spread_p50 > 0.0
        assert lm.spread_p95 > lm.spread_p50

    def test_spread_persistence_positive(self) -> None:
        lm = LiquidityMetrics(window=100)
        for i in range(50):
            lm.add_spread(100.0 if i % 2 == 0 else 10.0)
        assert abs(lm.spread_persistence) > 0.0

    def test_spread_persistence_no_data(self) -> None:
        lm = LiquidityMetrics(window=100)
        assert lm.spread_persistence == 0.0

    def test_depth_at_poc_median(self) -> None:
        lm = LiquidityMetrics(window=100)
        lm.add_depth(None)
        for depth in [100, 200, 300, 400, 500]:
            lm.add_depth(float(depth))
        assert lm.depth_at_poc_median == 300.0

    def test_no_data_returns_zero(self) -> None:
        lm = LiquidityMetrics(window=100)
        assert lm.spread_p50 == 0.0
        assert lm.spread_p95 == 0.0
        assert lm.depth_at_poc_median == 0.0


class TestFlowMetrics:
    """Tests for FlowMetrics aggregation."""

    def test_update_with_snapshot(self) -> None:
        snap = ConsolidatedSnapshot(
            symbol="TEST",
            underlying_symbol="TEST",
            updated_at=datetime.now(UTC),
            bid_price=Decimal("100.00"),
            ask_price=Decimal("101.00"),
            mid_price=Decimal("100.50"),
        )
        flow = FlowMetrics(symbol="TEST")
        flow.update(snap, 100.5, 10)
        assert flow.prev_mid_price == 100.5
        assert flow.vpin.vpin == 0.5

    def test_update_without_snapshot(self) -> None:
        snap = ConsolidatedSnapshot(
            symbol="TEST",
            underlying_symbol="TEST",
            updated_at=datetime.now(UTC),
        )
        flow = FlowMetrics(symbol="TEST")
        # No bid/ask on snapshot — flow updates should still work
        flow.update(snap, 100.0, 10)
        assert flow.vpin.vpin == 0.5
