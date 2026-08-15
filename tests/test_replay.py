"""Tests for Sprint 7: Replay framework."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dxlink_scanner.replay.replay_engine import (
    init_replay_models,
    load_events_from_parquet,
    replay_date_partition,
)


class TestLoadEventsFromParquet:
    """Tests for loading ConsolidatedEvent objects from parquet files."""

    def test_load_valid_parquet(self, tmp_path: Path) -> None:
        """Load events from a valid parquet file."""
        table = pa.table({
            "event_id": [1, 2, 3],
            "received_at": ["2024-01-15T10:30:00Z", "2024-01-15T10:30:01Z", "2024-01-15T10:30:02Z"],
            "source_type": ["TIME_AND_SALE", "TIME_AND_SALE", "QUOTE"],
            "symbol": ["SPY", "SPY", "SPY"],
            "last_trade_price": ["100.50", "101.00", None],
            "last_trade_size": [100, 200, None],
            "last_trade_type": ["regular", "regular", None],
            "bid_price": [None, None, "100.00"],
            "ask_price": [None, None, "101.00"],
            "event_time_ms": [1705314600000, 1705314601000, 1705314602000],
        })
        parquet_path = tmp_path / "events.parquet"
        pq.write_table(table, str(parquet_path))

        events = load_events_from_parquet(parquet_path)
        assert len(events) == 3
        assert events[0].symbol == "SPY"
        assert events[0].source_type == "TIME_AND_SALE"
        assert events[0].event_id == 1

    def test_load_empty_parquet(self, tmp_path: Path) -> None:
        table = pa.table({"symbol": [], "source_type": []})
        parquet_path = tmp_path / "empty.parquet"
        pq.write_table(table, str(parquet_path))
        events = load_events_from_parquet(parquet_path)
        assert events == []

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        events = load_events_from_parquet(tmp_path / "nonexistent.parquet")
        assert events == []

    def test_events_sorted_by_timestamp(self, tmp_path: Path) -> None:
        table = pa.table({
            "event_id": [3, 1, 2],
            "symbol": ["SPY", "SPY", "SPY"],
            "source_type": ["TIME_AND_SALE", "TIME_AND_SALE", "TIME_AND_SALE"],
            "event_time_ms": [1705314602000, 1705314600000, 1705314601000],
        })
        parquet_path = tmp_path / "events.parquet"
        pq.write_table(table, str(parquet_path))
        events = load_events_from_parquet(parquet_path)
        assert events[0].event_id == 1  # Sorted by timestamp
        assert events[1].event_id == 2
        assert events[2].event_id == 3

    def test_load_partial_columns(self, tmp_path: Path) -> None:
        """Load should handle parquet missing some columns gracefully."""
        table = pa.table({
            "event_id": [1],
            "symbol": ["TEST"],
            "source_type": ["TIME_AND_SALE"],
            "last_trade_price": ["100.00"],
            "last_trade_size": [100],
        })
        parquet_path = tmp_path / "events.parquet"
        pq.write_table(table, str(parquet_path))
        events = load_events_from_parquet(parquet_path)
        assert len(events) == 1
        assert events[0].last_trade_size == 100

    def test_decimal_conversion(self, tmp_path: Path) -> None:
        from decimal import Decimal
        table = pa.table({
            "event_id": [1],
            "symbol": ["TEST"],
            "source_type": ["QUOTE"],
            "bid_price": ["100.00"],
            "ask_price": ["101.00"],
            "event_time_ms": [1705314600000],
        })
        parquet_path = tmp_path / "events.parquet"
        pq.write_table(table, str(parquet_path))
        events = load_events_from_parquet(parquet_path)
        assert events[0].bid_price == Decimal("100.00")
        assert events[0].ask_price == Decimal("101.00")


class TestInitReplayModels:
    """Tests for replay model initialization."""

    def test_init_models(self) -> None:
        models = init_replay_models(["SPY", "QQQ"])
        assert "SPY" in models["bayesian_models"]
        assert "QQQ" in models["hawkes_models"]
        assert "SPY" in models["vap_models"]
        assert "QQQ" in models["flow_metrics"]
        assert "default" in models["bayesian_models"]
        assert models["cross_asset_hawkes"].symbols == ["SPY", "QQQ", "default"]

    def test_regime_detectors_initialized(self) -> None:
        models = init_replay_models(["SPY"])
        assert "SPY" in models["regime_detectors"]

    def test_model_sets_created(self) -> None:
        models = init_replay_models(["SPY"])
        assert "SPY" in models["model_sets"]
        assert models["model_sets"]["SPY"].bayesian.n_observations == 0


class TestReplayDatePartition:
    """Tests for replaying a date partition of parquet files."""

    def test_replay_empty_partition(self, tmp_path: Path) -> None:
        """Replay of a partition with no parquet files should return error."""
        result = asyncio.run(replay_date_partition(tmp_path))
        assert "error" in result
        assert result["error"] == "no files found"

    @pytest.mark.asyncio
    async def test_replay_with_events(self, tmp_path: Path) -> None:
        """Replay should process events and return summary."""
        # Create a parquet file with TAS events
        table = pa.table({
            "event_id": [1, 2, 3],
            "symbol": ["SPY", "SPY", "SPY"],
            "source_type": ["QUOTE", "TIME_AND_SALE", "TIME_AND_SALE"],
            "bid_price": ["100.00", None, None],
            "ask_price": ["101.00", None, None],
            "last_trade_price": [None, "100.50", "100.75"],
            "last_trade_size": [None, 100, 200],
            "event_time_ms": [1705314600000, 1705314601000, 1705314602000],
        })
        parquet_path = tmp_path / "events_v1_test.parquet"
        pq.write_table(table, str(parquet_path))

        result = await replay_date_partition(tmp_path, None)
        assert result["events_processed"] == 3
        assert result["model_updates"] == 2  # 2 TAS events
        assert result["date"] == tmp_path.name
