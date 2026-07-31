"""Tests for Sprint 5 components: dynamic strikes, compaction, schema v2."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dxlink_scanner.config import TickerConfig, WatchlistConfig
from dxlink_scanner.dynamic_strikes import DynamicStrikeManager


@pytest.fixture
def watchlist() -> WatchlistConfig:
    return WatchlistConfig(
        tickers=[
            TickerConfig(symbol="SPY", option_type="equity", strikes_around_atm=10),
        ]
    )


@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock()


class TestDynamicStrikeManager:
    @pytest.mark.asyncio
    async def test_initial_scan_returns_delta(self, mock_session, watchlist) -> None:
        mgr = DynamicStrikeManager(mock_session, watchlist)

        # Mock the chain loader
        mgr._collect_symbols = AsyncMock(return_value=("SPY", ["SPY250731C00500000", "SPY250731C00505000"], "SPY"))

        delta = await mgr.initial_scan()
        assert delta.added == ["SPY250731C00500000", "SPY250731C00505000"]
        assert delta.removed == []
        assert mgr._last_scan is not None

    @pytest.mark.asyncio
    async def test_rescan_no_change(self, mock_session, watchlist) -> None:
        mgr = DynamicStrikeManager(mock_session, watchlist, rescan_interval_min=0)
        mgr._collect_symbols = AsyncMock(return_value=("SPY", ["SYM1", "SYM2"], "SPY"))
        await mgr.initial_scan()
        delta = await mgr.rescan()
        assert delta is not None
        assert delta.added == []
        assert delta.removed == []

    @pytest.mark.asyncio
    async def test_rescan_with_change(self, mock_session, watchlist) -> None:
        mgr = DynamicStrikeManager(mock_session, watchlist, rescan_interval_min=0)
        mgr._collect_symbols = AsyncMock(return_value=("SPY", ["SYM1", "SYM2"], "SPY"))
        await mgr.initial_scan()

        mgr._collect_symbols = AsyncMock(return_value=("SPY", ["SYM1", "SYM3"], "SPY"))
        delta = await mgr.rescan()
        assert delta is not None
        assert "SYM3" in delta.added
        assert "SYM2" in delta.removed

    @pytest.mark.asyncio
    async def test_should_rescan_timing(self, mock_session, watchlist) -> None:
        mgr = DynamicStrikeManager(mock_session, watchlist, rescan_interval_min=60)
        assert mgr.should_rescan() is True  # No scan yet

        mgr._last_scan = dt.datetime.now(dt.UTC)
        assert mgr.should_rescan() is False  # Not enough time passed

        mgr._last_scan = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=61)
        assert mgr.should_rescan() is True

    @pytest.mark.asyncio
    async def test_all_symbols(self, mock_session, watchlist) -> None:
        mgr = DynamicStrikeManager(mock_session, watchlist)
        mgr._collect_symbols = AsyncMock(return_value=("SPY", ["SYM1", "SYM2"], "SPY"))
        await mgr.initial_scan()
        assert mgr.all_symbols == ["SYM1", "SYM2"]


class TestCompactParquet:
    def _make_test_parquet(self, path: Path, rows: int) -> None:
        """Create a test parquet file with sample rows."""
        schema = pa.schema(
            [
                ("event_id", pa.int64()),
                ("received_at", pa.string()),
                ("source_type", pa.string()),
                ("symbol", pa.string()),
                ("bid_price", pa.string()),
                ("ask_price", pa.string()),
                ("last_trade_price", pa.string()),
                ("last_trade_size", pa.int32()),
                ("last_trade_time", pa.int64()),
                ("last_trade_type", pa.string()),
                ("event_time_ms", pa.int64()),
            ]
        )
        data = []
        for i in range(rows):
            data.append(
                {
                    "event_id": i,
                    "received_at": dt.datetime.now(dt.UTC).isoformat(),
                    "source_type": "TIME_AND_SALE",
                    "symbol": f"SPY{i}",
                    "bid_price": "100.00",
                    "ask_price": "100.50",
                    "last_trade_price": "100.25",
                    "last_trade_size": 50,
                    "last_trade_time": 1705315800000,
                    "last_trade_type": "regular",
                    "event_time_ms": 1705315800000,
                }
            )
        table = pa.Table.from_pylist(data, schema=schema)
        pq.write_table(table, str(path))

    def test_compact_merges_files(self, tmp_path: Path) -> None:
        from scripts.compact_parquet import compact_date_partition

        partition = tmp_path / "2024-07-31"
        partition.mkdir()

        # Create 3 small files
        for i in range(3):
            self._make_test_parquet(partition / f"events_v1_abc_{i}.parquet", 5)

        written = compact_date_partition(partition, target_size=1024 * 1024)
        assert written == 1  # All 15 rows should fit in one small file

        # Verify compacted file
        compacted_files = list(partition.glob("events_v1_compact_*.parquet"))
        assert len(compacted_files) == 1
        table = pq.read_table(str(compacted_files[0]))
        assert table.num_rows == 15

        # Original files should be deleted
        original_files = list(partition.glob("events_v1_abc_*.parquet"))
        assert len(original_files) == 0

    def test_compact_updates_meta(self, tmp_path: Path) -> None:
        from scripts.compact_parquet import compact_date_partition

        partition = tmp_path / "2024-07-31"
        partition.mkdir()
        self._make_test_parquet(partition / "events_v1_abc_0.parquet", 3)

        # Create session meta
        meta = {"session_id": "test", "event_count": 3, "schema_version": "v1"}
        (partition / "session_meta.json").write_text(json.dumps(meta, indent=2))

        compact_date_partition(partition, target_size=1024 * 1024)

        # Verify meta updated
        updated_meta = json.loads((partition / "session_meta.json").read_text())
        assert updated_meta["compacted_files"] == 1
        assert "compacted_at" in updated_meta

    def test_compact_empty_dir(self, tmp_path: Path) -> None:
        from scripts.compact_parquet import compact_date_partition

        partition = tmp_path / "empty"
        partition.mkdir()
        result = compact_date_partition(partition)
        assert result == 0

    def test_compact_multiple_files(self, tmp_path: Path) -> None:
        """Test compaction of multiple files into one."""
        from scripts.compact_parquet import compact_date_partition

        partition = tmp_path / "2024-07-31"
        partition.mkdir()

        for i in range(3):
            self._make_test_parquet(partition / f"events_v1_abc_{i}.parquet", 5)

        compact_date_partition(partition, target_size=1)
        # With tiny target, should create at least 1 file
        compacted_files = sorted(partition.glob("events_v1_compact_*.parquet"))
        assert len(compacted_files) >= 1
        table = pq.read_table(str(compacted_files[0]))
        assert table.num_rows > 0

    def test_find_date_partitions(self, tmp_path: Path) -> None:
        from scripts.compact_parquet import find_date_partitions

        (tmp_path / "2024-07-31").mkdir()
        (tmp_path / "2024-08-01").mkdir()
        (tmp_path / "not-a-date").mkdir()
        (tmp_path / "events_v1_abc.parquet").touch()

        partitions = find_date_partitions(tmp_path)
        assert len(partitions) == 2


class TestSchemaV2:
    def test_schema_v2_has_derived_fields(self) -> None:
        from dxlink_scanner.schemas.v2 import schema_v2, v2_new_fields

        field_names = [f.name for f in schema_v2]
        for field in v2_new_fields:
            assert field in field_names, f"Missing {field} in schema v2"

    def test_schema_v2_extends_v1(self) -> None:
        from dxlink_scanner.schemas.v1 import schema_v1
        from dxlink_scanner.schemas.v2 import schema_v2, v1_fields

        v2_names = [f.name for f in schema_v2]
        v1_names = [f.name for f in schema_v1]

        # All v1 fields must be in v2
        for name in v1_names:
            assert name in v2_names, f"v1 field {name} missing in v2"

        # v1 fields list should match schema_v1
        assert len(v1_fields) == len(v1_names)
