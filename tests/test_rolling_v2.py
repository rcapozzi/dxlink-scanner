"""Comprehensive tests for RollingStatsV2."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from dxlink_scanner.config import DetectionConfig
from dxlink_scanner.stats.rolling_v2 import RollingStatsManagerV2, RollingStatsV2


def test_v2_basic_add_and_median():
    """Test basic add and median operations."""
    stats = RollingStatsV2(symbol="TEST", window_size=5)

    for val in [10, 20, 30, 40, 50]:
        stats.add(val)

    assert stats.median() == 30.0
    assert stats.count == 5


def test_v2_window_eviction():
    """Test sliding window eviction."""
    stats = RollingStatsV2(symbol="TEST", window_size=3)

    stats.add(10)
    stats.add(20)
    stats.add(30)
    stats.add(40)  # Should evict 10

    assert stats.count == 3
    assert stats.median() == 30.0  # [20, 30, 40] -> median 30


def test_v2_percentile():
    """Test exact percentile calculation."""
    stats = RollingStatsV2(symbol="TEST", window_size=10)

    for val in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        stats.add(val)

    assert stats.percentile(0) == 10.0
    assert stats.percentile(100) == 100.0
    assert stats.percentile(50) == 55.0  # median of even count
    assert abs(stats.percentile(25) - 32.5) < 1e-10  # linear interpolation
    assert abs(stats.percentile(75) - 77.5) < 1e-10
    assert abs(stats.percentile(90) - 91.0) < 1e-10
    assert abs(stats.percentile(95) - 95.5) < 1e-10
    assert abs(stats.percentile(99) - 99.1) < 1e-10


def test_v2_mad():
    """Test exact MAD calculation."""
    stats = RollingStatsV2(symbol="TEST", window_size=5)

    for val in [10, 20, 30, 40, 50]:
        stats.add(val)

    # median = 30, abs deviations = [20, 10, 0, 10, 20] -> sorted [0, 10, 10, 20, 20] -> median = 10
    assert stats.mad() == 10.0


def test_v2_weighted_mean():
    """Test weighted mean with half-life."""
    stats = RollingStatsV2(symbol="TEST", window_size=10, half_life_sec=3600)
    now = dt.datetime.now(dt.UTC)

    stats.add(100, now)
    stats.add(200, now + dt.timedelta(seconds=1800))  # 0.5 half-life

    # Should be weighted toward newer value
    mean = stats.mean()
    assert mean >= 150.0  # Should be weighted toward 200 (newer value)
    assert mean <= 200.0


def test_v2_z_score():
    """Test z-score calculation."""
    stats = RollingStatsV2(symbol="TEST", window_size=5)

    for val in [10, 20, 30, 40, 50]:
        stats.add(val)

    # mean=30, std~15.8, z-score of 50 = (50-30)/15.8 ≈ 1.26
    z = stats.z_score(50)
    assert 1.0 < z < 1.5


def test_v2_modified_z_score():
    """Test modified z-score using MAD."""
    stats = RollingStatsV2(symbol="TEST", window_size=5)

    for val in [10, 20, 30, 40, 50]:
        stats.add(val)

    # median=30, mad=10, modified_z = 0.6745 * (50-30)/10 = 1.349
    mz = stats.modified_z_score(50)
    assert 1.3 < mz < 1.4


def test_v2_session_aware():
    """Test session-aware RTH/ETH separation."""
    stats = RollingStatsV2(symbol="TEST", window_size=10, session_aware=True)

    et_tz = ZoneInfo("America/New_York")
    rth_time = dt.datetime(2024, 1, 1, 14, 30, tzinfo=et_tz)  # 14:30 ET = RTH
    eth_time = dt.datetime(2024, 1, 1, 20, 0, tzinfo=et_tz)  # 20:00 ET = ETH

    stats.add(100, rth_time)
    stats.add(200, rth_time)
    stats.add(50, eth_time)

    # RTH median should be 150, ETH median should be 50
    assert stats.rth_median() == 150.0
    assert stats.eth_median() == 50.0
    # Overall median of combined [100, 200, 50] -> sorted [50, 100, 200] -> median 100
    assert stats.median() == 100.0


def test_v2_session_aware_separate_windows():
    """Test that RTH and ETH maintain separate windows."""
    stats = RollingStatsV2(symbol="TEST", window_size=2, session_aware=True)

    et_tz = ZoneInfo("America/New_York")
    rth_time = dt.datetime(2024, 1, 1, 14, 30, tzinfo=et_tz)
    eth_time = dt.datetime(2024, 1, 1, 20, 0, tzinfo=et_tz)

    stats.add(100, rth_time)
    stats.add(200, rth_time)
    stats.add(300, rth_time)  # Should evict 100 from RTH
    stats.add(50, eth_time)
    stats.add(60, eth_time)
    stats.add(70, eth_time)  # Should evict 50 from ETH

    # RTH window should have [200, 300], ETH window [60, 70]
    assert stats.rth_median() == 250.0
    assert stats.eth_median() == 65.0


def test_v2_reset():
    """Test reset/clear functionality."""
    stats = RollingStatsV2(symbol="TEST", window_size=5)

    for val in [10, 20, 30]:
        stats.add(val)

    stats.reset()

    assert stats.count == 0
    assert stats.median() == 0.0
    assert stats.mean() == 0.0


def test_v2_manager_reset_remove():
    """Test manager reset and remove operations."""
    config = DetectionConfig()
    mgr = RollingStatsManagerV2(config)

    mgr.add("SPY", 100)
    mgr.add("SPY", 200)

    assert mgr.get("SPY").count == 2

    mgr.reset("SPY")
    assert mgr.get("SPY").count == 0

    mgr.add("SPY", 300)
    assert mgr.get("SPY").count == 1

    assert mgr.remove("SPY") is True
    assert mgr.get("SPY") is None
    assert mgr.remove("NONEXISTENT") is False


def test_v2_manager_clear_all():
    """Test manager clear_all."""
    config = DetectionConfig()
    mgr = RollingStatsManagerV2(config)

    mgr.add("SPY", 100)
    mgr.add("QQQ", 200)

    mgr.clear_all()

    assert mgr.get("SPY") is None
    assert mgr.get("QQQ") is None


def test_v2_is_anomalous():
    """Test anomaly detection."""
    config = DetectionConfig(size_mult=2.0, abs_min_size=10)
    mgr = RollingStatsManagerV2(config)

    # Warm up
    for val in [10, 15, 20, 25, 30]:
        mgr.add("SPY", val)

    # median=20, size_mult=2.0 -> threshold=40
    triggered, ratio, med = mgr.is_anomalous("SPY", 50)
    assert triggered is True
    assert ratio == 2.5
    assert med == 20.0

    triggered, ratio, med = mgr.is_anomalous("SPY", 30)
    assert triggered is False


def test_v2_rth_eth_getters():
    """Test RTH/ETH specific getters."""
    stats = RollingStatsV2(symbol="TEST", window_size=10, session_aware=True)

    et_tz = ZoneInfo("America/New_York")
    rth_time = dt.datetime(2024, 1, 1, 14, 30, tzinfo=et_tz)
    eth_time = dt.datetime(2024, 1, 1, 20, 0, tzinfo=et_tz)

    stats.add(100, rth_time)
    stats.add(200, rth_time)
    stats.add(50, eth_time)

    assert stats.rth_median() == 150.0
    assert stats.rth_mean() == 150.0
    assert stats.rth_std() > 0
    assert stats.eth_median() == 50.0
    assert stats.eth_mean() == 50.0
    assert stats.eth_std() == 0.0  # single value


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
