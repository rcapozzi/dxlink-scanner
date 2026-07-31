"""Tests for the rolling statistics manager."""

from dxlink_scanner.config import DetectionConfig
from dxlink_scanner.stats import RollingStatsManager


def test_add_and_median():
    """Test adding sizes and computing median."""
    cfg = DetectionConfig()
    mgr = RollingStatsManager(cfg)
    for s in [10, 20, 30, 40, 50]:
        mgr.add("SYMBOL", s)
    stat = mgr.get("SYMBOL")
    assert stat is not None
    assert stat.median() == 30.0


def test_is_anomalous_threshold():
    """Test that a large print triggers an anomaly."""
    cfg = DetectionConfig(size_mult=5.0, abs_min_size=10, stats_window=50)
    mgr = RollingStatsManager(cfg)
    for s in [10] * 50:
        mgr.add("SYMBOL", s)
    triggered, ratio, med = mgr.is_anomalous("SYMBOL", 100)
    assert triggered is True
    assert ratio == 10.0
    assert med == 10.0


def test_not_anomalous_small_print():
    """Test that a small print does not trigger."""
    cfg = DetectionConfig(size_mult=5.0, abs_min_size=10, stats_window=50)
    mgr = RollingStatsManager(cfg)
    for s in [100] * 50:
        mgr.add("SYMBOL", s)
    triggered, ratio, med = mgr.is_anomalous("SYMBOL", 20)
    assert triggered is False
    assert ratio == 0.2


def test_no_stats_yet():
    """Test behavior when no stats exist yet."""
    cfg = DetectionConfig(size_mult=5.0, abs_min_size=10, stats_window=50)
    mgr = RollingStatsManager(cfg)
    triggered, ratio, med = mgr.is_anomalous("NEW", 15)
    assert triggered is True  # 15 >= abs_min_size=10
    assert ratio == 0.0
    assert med == 0.0
