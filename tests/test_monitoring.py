"""Tests for Sprint 7: Monitoring — data quality, model health, replay."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from dxlink_scanner.monitoring import (
    DataQualityMonitor,
    ModelHealthMonitor,
    ModelParamTracker,
)
from dxlink_scanner.stats import ModelSet


class TestDataQualityMonitor:
    """Tests for gap detection and schema drift monitoring."""

    def test_no_gap_on_first_event(self) -> None:
        dqm = DataQualityMonitor(max_gap_sec=60.0)
        ts = dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=dt.UTC)
        result = dqm.record_event("SPY", ts)
        assert result is None  # First event per symbol, no gap

    def test_gap_detected(self) -> None:
        dqm = DataQualityMonitor(max_gap_sec=60.0)
        ts1 = dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=dt.UTC)
        ts2 = dt.datetime(2024, 1, 15, 10, 32, 0, tzinfo=dt.UTC)  # 2 minutes later
        dqm.record_event("SPY", ts1)
        result = dqm.record_event("SPY", ts2)
        assert result is not None
        assert result.gap_duration_sec == 120.0
        assert result.symbol == "SPY"

    def test_no_gap_within_threshold(self) -> None:
        dqm = DataQualityMonitor(max_gap_sec=60.0)
        ts1 = dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=dt.UTC)
        ts2 = dt.datetime(2024, 1, 15, 10, 30, 30, tzinfo=dt.UTC)  # 30s later
        dqm.record_event("SPY", ts1)
        result = dqm.record_event("SPY", ts2)
        assert result is None

    def test_gap_in_parquet(self, tmp_path: Path) -> None:
        dqm = DataQualityMonitor(max_gap_sec=60.0)
        # Create a parquet file with symbol + event_time_ms columns
        table = pa.table({
            "symbol": ["SPY", "SPY", "QQQ", "QQQ"],
            "event_time_ms": [1700000000000, 1700000090000, 1700000000000, 1700000000000],
        })
        parquet_path = tmp_path / "test.parquet"
        pq.write_table(table, str(parquet_path))
        gaps = dqm.detect_gaps_in_parquet(parquet_path)
        assert len(gaps) >= 1  # SPY has a 90s gap

    def test_schema_drift_detected(self, tmp_path: Path) -> None:
        dqm = DataQualityMonitor()
        table = pa.table({"a": [1], "b": [2], "extra": [3]})
        parquet_path = tmp_path / "test.parquet"
        pq.write_table(table, str(parquet_path))
        report = dqm.check_schema_drift(parquet_path, expected_fields=["a", "b", "c"])
        assert report is not None
        assert "c" in report.missing_fields
        assert "extra" in report.unexpected_fields

    def test_no_schema_drift(self, tmp_path: Path) -> None:
        dqm = DataQualityMonitor()
        table = pa.table({"a": [1], "b": [2]})
        parquet_path = tmp_path / "test.parquet"
        pq.write_table(table, str(parquet_path))
        report = dqm.check_schema_drift(parquet_path, expected_fields=["a", "b"])
        assert report is None

    def test_reset(self) -> None:
        dqm = DataQualityMonitor()
        ts = dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=dt.UTC)
        dqm.record_event("SPY", ts)
        dqm.reset()
        assert not dqm._last_event_time
        assert not dqm._gaps


class TestModelParamTracker:
    """Tests for model parameter outlier detection."""

    def test_no_outliers_initially(self) -> None:
        tracker = ModelParamTracker()
        outliers = tracker.record("SPY", {"alpha_post": 5.0, "beta_post": 10.0})
        assert outliers == []

    def test_outlier_detected(self) -> None:
        tracker = ModelParamTracker(z_threshold=3.0)
        # Record 10 values around 5.0
        for i in range(10):
            tracker.record("SPY", {"alpha_post": 5.0 + i * 0.01})
        # Now record an outlier at 100.0
        outliers = tracker.record("SPY", {"alpha_post": 100.0})
        assert len(outliers) >= 1
        assert outliers[0].parameter == "alpha_post"
        assert outliers[0].z_score > 3.0

    def test_no_outlier_within_threshold(self) -> None:
        tracker = ModelParamTracker(z_threshold=5.0)  # High threshold
        for i in range(10):
            tracker.record("SPY", {"alpha_post": 5.0 + i * 0.1})
        outliers = tracker.record("SPY", {"alpha_post": 6.0})  # Close to mean
        assert outliers == []

    def test_get_history(self) -> None:
        tracker = ModelParamTracker(history_window=50)
        for i in range(10):
            tracker.record("SPY", {"alpha_post": float(i)})
        history = tracker.get_history("SPY", "alpha_post")
        assert len(history) == 10
        assert history[-1] == 9.0

    def test_clear(self) -> None:
        tracker = ModelParamTracker()
        tracker.record("SPY", {"alpha_post": 5.0})
        tracker.clear()
        assert tracker.get_history("SPY", "alpha_post") == []


class TestModelHealthMonitor:
    """Tests for model health snapshot generation."""

    def test_check_all(self) -> None:
        ms = ModelSet()
        ms.bayesian.update(10)
        ms.hawkes.add_event(1000.0)
        model_sets = {"SPY": ms}
        monitor = ModelHealthMonitor(model_sets)
        snapshots = monitor.check_all()
        assert len(snapshots) == 1
        assert snapshots[0].symbol == "SPY"
        assert snapshots[0].n_observations == 1
        assert snapshots[0].posterior_mean is not None
        assert snapshots[0].health_score > 0.0

    def test_check_all_empty(self) -> None:
        monitor = ModelHealthMonitor({})
        snapshots = monitor.check_all()
        assert snapshots == []

    def test_health_with_observations(self) -> None:
        ms = ModelSet()
        ms.bayesian.alpha = 10.0
        ms.bayesian.beta = 10.0
        ms.bayesian.update(20)
        model_sets = {"TEST": ms}
        monitor = ModelHealthMonitor(model_sets, coverage_tolerance=0.5)
        # Provide observations near the posterior mean (≈ 3.0)
        observations = {"TEST": [3.0, 3.0, 3.0, 3.0, 3.0, 4.0, 3.0, 3.0, 4.0, 3.0]}
        snapshots = monitor.check_all(recent_observations=observations)
        assert snapshots[0].coverage_rate is not None
        assert snapshots[0].calibration_status in ("good", "warning", "critical", "unknown")

    def test_to_json(self) -> None:
        ms = ModelSet()
        ms.bayesian.update(5)
        monitor = ModelHealthMonitor({"SPY": ms})
        snapshots = monitor.check_all()
        json_str = monitor.to_json(snapshots)
        import json
        data = json.loads(json_str)
        assert len(data) == 1
        assert data[0]["symbol"] == "SPY"

    def test_to_dict(self) -> None:
        ms = ModelSet()
        ms.bayesian.update(5)
        monitor = ModelHealthMonitor({"SPY": ms})
        snapshots = monitor.check_all()
        result = monitor.to_dict(snapshots)
        assert "checked_at" in result
        assert result["symbols_checked"] == 1
        assert len(result["health"]) == 1
