"""Tests for consolidated event model and schema."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from dxlink_scanner.models import (
    ConsolidatedEvent,
    ConsolidatedSnapshot,
    _parse_dt,
    _to_epoch_ms,
    merge_into_snapshot,
)
from dxlink_scanner.schemas.v1 import schema_v1


class TestToEpochMs:
    def test_none(self) -> None:
        assert _to_epoch_ms(None) is None

    def test_iso_string(self) -> None:
        result = _to_epoch_ms("2024-01-15T10:30:00Z")
        assert result is not None
        assert result > 0

    def test_datetime(self) -> None:
        ts = dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=dt.UTC)
        result = _to_epoch_ms(ts)
        assert result == 1705314600000

    def test_int(self) -> None:
        assert _to_epoch_ms(1705315800000) == 1705315800000

    def test_float(self) -> None:
        assert _to_epoch_ms(1705315800123.0) == 1705315800123


class TestParseDt:
    def test_none(self) -> None:
        assert _parse_dt(None) is None

    def test_int_epoch_ms(self) -> None:
        result = _parse_dt(1705315800000)
        assert result is not None
        assert result.year == 2024

    def test_datetime_passthrough(self) -> None:
        ts = dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=dt.UTC)
        assert _parse_dt(ts) == ts


class TestSnapshotMerge:
    """Quote-only snapshot merge tests."""

    def _make_snap(self) -> ConsolidatedSnapshot:
        return ConsolidatedSnapshot(
            symbol="SPY250731C00450000",
            underlying_symbol="SPY",
            updated_at=dt.datetime.now(dt.UTC),
        )

    def _make_quote_event(self) -> ConsolidatedEvent:
        return ConsolidatedEvent(
            event_id=1,
            received_at=dt.datetime.now(dt.UTC),
            source_type="QUOTE",
            symbol="SPY250731C00450000",
            bid_price=Decimal("100.00"),
            ask_price=Decimal("101.00"),
            event_time_ms=1705315800000,
        )

    def _make_tas_event(self) -> ConsolidatedEvent:
        return ConsolidatedEvent(
            event_id=3,
            received_at=dt.datetime.now(dt.UTC),
            source_type="TIME_AND_SALE",
            symbol="SPY250731C00450000",
            last_trade_price=Decimal("100.75"),
            last_trade_size=100,
            last_trade_type="regular",
            event_time_ms=1705315802000,
        )

    def test_merge_quote_first(self) -> None:
        snap = self._make_snap()
        merge_into_snapshot(snap, self._make_quote_event())
        assert snap.bid_price == Decimal("100.00")
        assert snap.ask_price == Decimal("101.00")
        assert snap.mid_price == Decimal("100.50")
        assert snap.spread == Decimal("1.00")
        assert snap.spread_bps == pytest.approx(100.0, rel=1e-2)

    def test_merge_tas_updates_last_trade(self) -> None:
        """TAS events update last_trade_* fields and trade_vs_mid."""
        snap = self._make_snap()
        merge_into_snapshot(snap, self._make_quote_event())
        merge_into_snapshot(snap, self._make_tas_event())
        assert snap.last_trade_price == Decimal("100.75")
        assert snap.last_trade_size == 100
        assert snap.trade_vs_mid == Decimal("0.25")

    def test_merge_tas_then_quote_computes_trade_vs_mid(self) -> None:
        """TAS first, then Quote — trade_vs_mid computed after Quote arrives."""
        snap = self._make_snap()
        merge_into_snapshot(snap, self._make_tas_event())
        merge_into_snapshot(snap, self._make_quote_event())
        assert snap.last_trade_price == Decimal("100.75")
        assert snap.mid_price == Decimal("100.50")
        assert snap.trade_vs_mid == Decimal("0.25")

    def test_merge_quote_updates_bid_ask(self) -> None:
        """Later Quote should overwrite earlier Quote."""
        snap = self._make_snap()
        merge_into_snapshot(snap, self._make_quote_event())
        # Updated quote
        event2 = ConsolidatedEvent(
            event_id=2,
            received_at=dt.datetime.now(dt.UTC),
            source_type="QUOTE",
            symbol="SPY250731C00450000",
            bid_price=Decimal("100.50"),
            ask_price=Decimal("101.50"),
            event_time_ms=1705315801000,
        )
        merge_into_snapshot(snap, event2)
        assert snap.bid_price == Decimal("100.50")
        assert snap.ask_price == Decimal("101.50")
        assert snap.mid_price == Decimal("101.00")

    def test_no_bid_ask_no_derived(self) -> None:
        """Without bid/ask, derived fields should be None."""
        snap = self._make_snap()
        merge_into_snapshot(snap, self._make_tas_event())
        assert snap.mid_price is None
        assert snap.spread is None
        assert snap.spread_bps is None

    def test_spread_zero_no_bps(self) -> None:
        """When mid_price is zero, spread_bps should be None, not error."""
        snap = self._make_snap()
        event = ConsolidatedEvent(
            event_id=1,
            received_at=dt.datetime.now(dt.UTC),
            source_type="QUOTE",
            symbol="TEST",
            bid_price=Decimal("0.00"),
            ask_price=Decimal("0.00"),
        )
        merge_into_snapshot(snap, event)
        assert snap.spread_bps is None


class TestSnapshotSchema:
    def test_schema_v1_exists(self) -> None:
        assert schema_v1 is not None

    def test_schema_v1_has_all_fields(self) -> None:
        field_names = {f.name for f in schema_v1}
        expected = {
            "event_id",
            "received_at",
            "source_type",
            "symbol",
            "bid_price",
            "ask_price",
            "last_trade_price",
            "last_trade_size",
            "last_trade_time",
            "last_trade_type",
            "theo_price",
            "underlying_price",
            "delta",
            "gamma",
            "dividend",
            "interest",
            "event_time_ms",
            "time_ms",
            "time_nano_part_ms",
            "evict_at",
        }
        assert expected == field_names

    def test_schema_v1_fields_nullable(self) -> None:
        # All fields except event_id should be nullable
        field_map = {f.name: f for f in schema_v1}
        assert field_map["event_id"].nullable is True  # int64 allows null
        assert field_map["symbol"].nullable is True
        assert field_map["bid_price"].nullable is True


class TestSnapshotStore:
    def test_ingest_and_get(self) -> None:
        from dxlink_scanner.config import StreamConfig
        from dxlink_scanner.snapshot_store import SnapshotStore

        config = StreamConfig()
        store = SnapshotStore(config, persist=False)
        event = ConsolidatedEvent(
            event_id=1,
            received_at=dt.datetime.now(dt.UTC),
            source_type="TIME_AND_SALE",
            symbol="SPY250731C00450000",
            event_time_ms=1705315800000,
        )
        store.set_underlying_map({"SPY250731C00450000": "SPY"})
        store.ingest(event)
        snap = store.get("SPY250731C00450000")
        assert snap is not None
        assert snap.symbol == "SPY250731C00450000"
        assert snap.underlying_symbol == "SPY"
        assert store.snapshot_count == 1

    def test_ingest_multiple_updates(self) -> None:
        from dxlink_scanner.config import StreamConfig
        from dxlink_scanner.snapshot_store import SnapshotStore

        store = SnapshotStore(StreamConfig(), persist=False)
        event1 = ConsolidatedEvent(
            event_id=1,
            received_at=dt.datetime.now(dt.UTC),
            source_type="QUOTE",
            symbol="TEST",
            bid_price=Decimal("100.00"),
            ask_price=Decimal("101.00"),
        )
        event2 = ConsolidatedEvent(
            event_id=2,
            received_at=dt.datetime.now(dt.UTC),
            source_type="QUOTE",
            symbol="TEST",
            bid_price=Decimal("100.50"),
            ask_price=Decimal("101.50"),
        )
        store.ingest(event1)
        store.ingest(event2)
        snap = store.get("TEST")
        assert snap is not None
        assert snap.bid_price == Decimal("100.50")
        assert snap.ask_price == Decimal("101.50")

    @pytest.mark.asyncio
    async def test_flush_to_parquet(self, tmp_path) -> None:
        from dxlink_scanner.config import StreamConfig
        from dxlink_scanner.snapshot_store import SnapshotStore

        config = StreamConfig(flush_interval_sec=5.0)
        store = SnapshotStore(config, persist=True)
        store.set_underlying_map({"SYM": "UNDER"})

        event = ConsolidatedEvent(
            event_id=1,
            received_at=dt.datetime.now(dt.UTC),
            source_type="TIME_AND_SALE",
            symbol="SYM",
            event_time_ms=1705315800000,
        )
        store.ingest(event)
        output_dir = tmp_path / "data"
        await store.flush_remaining(output_dir)

        # Should have created a parquet file in today's date subdirectory
        today = str(dt.date.today())
        tables = list((output_dir / today).glob("*.parquet"))
        assert len(tables) >= 1
        import pyarrow.parquet as pq

        table = pq.read_table(str(tables[0]))
        assert table.num_rows == 1

    def test_set_evict_map(self) -> None:
        from dxlink_scanner.config import StreamConfig
        from dxlink_scanner.snapshot_store import SnapshotStore

        store = SnapshotStore(StreamConfig(), persist=False)
        store.set_evict_map({"SYM": 1705315800000})
        assert store._evict_map == {"SYM": 1705315800000}
