"""Tests for cli.py normalized event handling."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tastytrade.dxfeed import Quote, TimeAndSale

from dxlink_scanner.bootstrap import InstrumentResolver
from dxlink_scanner.cli import _get_normalize_fn, _timeandsale_to_event


class TestTimeAndSaleAdapter:
    def test_convert_string_timestamp(self) -> None:
        tas = MagicMock(spec=TimeAndSale)
        tas.event_symbol = "SPY250731C00450000"
        tas.price = 100.50
        tas.size = 100
        tas.event_time = "2024-07-31T10:30:00"
        tas.bid_price = 100.25
        tas.ask_price = 100.75
        tas.type = "regular"

        event = _timeandsale_to_event(tas)
        assert event.symbol == "SPY250731C00450000"
        assert event.price == Decimal("100.50")
        assert event.size == 100

    def test_convert_int_timestamp(self) -> None:
        tas = MagicMock(spec=TimeAndSale)
        tas.event_symbol = "SPY250731C00450000"
        tas.price = 100.00
        tas.size = 50
        tas.event_time = 1722418200000  # epoch ms
        tas.bid_price = None
        tas.ask_price = None
        tas.type = None

        event = _timeandsale_to_event(tas)
        assert event.symbol == "SPY250731C00450000"
        assert event.bid_price is None
        assert event.trade_type is None

    def test_convert_fallback_timestamp(self) -> None:
        tas = MagicMock(spec=TimeAndSale)
        tas.event_symbol = "TEST"
        tas.price = 0
        tas.size = 0
        tas.event_time = None
        tas.bid_price = None
        tas.ask_price = None
        tas.type = None

        event = _timeandsale_to_event(tas)
        assert isinstance(event.timestamp, dt.datetime)


class TestGetNormalizeFn:
    def test_quote(self) -> None:
        from dxlink_scanner.models import normalize_quote

        assert _get_normalize_fn(Quote) is normalize_quote

    def test_timeandsale(self) -> None:
        from dxlink_scanner.models import normalize_timeandsale

        assert _get_normalize_fn(TimeAndSale) is normalize_timeandsale

    def test_unknown_type(self) -> None:
        with pytest.raises(ValueError, match="Unknown event type"):
            _get_normalize_fn(str)  # type: ignore[arg-type]


class TestInstrumentResolver:
    def test_futures_streamer(self) -> None:
        result = InstrumentResolver.resolve_futures_streamer("ES")
        assert result == "/ES:XCME"

    def test_futures_unknown_defaults_xcme(self) -> None:
        result = InstrumentResolver.resolve_futures_streamer("ZZ")
        assert result == "/ZZ:XCME"

    def test_index_option_streamer(self) -> None:
        result = InstrumentResolver.resolve_index_option_streamer("SPX")
        assert result == "SPX"

    def test_futures_exchange_map(self) -> None:
        assert InstrumentResolver.resolve_futures_streamer("NQ") == "/NQ:XCME"
        assert InstrumentResolver.resolve_futures_streamer("CL") == "/CL:NYMEX"
        assert InstrumentResolver.resolve_futures_streamer("GC") == "/GC:COMEX"

    def test_futures_streamer_with_leading_slash(self) -> None:
        """Product codes may arrive with a leading slash (e.g. '/ES').
        Must produce '/ES:XCME', not '//ES:XCME'.
        """
        assert InstrumentResolver.resolve_futures_streamer("/ES") == "/ES:XCME"
        assert InstrumentResolver.resolve_futures_streamer("/NQ") == "/NQ:XCME"


class TestStreamConfig:
    def test_defaults(self) -> None:
        from dxlink_scanner.config import StreamConfig

        sc = StreamConfig()
        assert sc.backpressure_queue_size == 500
        assert sc.flush_interval_sec == 5.0
        assert sc.flush_batch_size == 10000

    def test_validation_gt_zero(self) -> None:
        from pydantic import ValidationError

        from dxlink_scanner.config import StreamConfig

        with pytest.raises(ValidationError):
            StreamConfig(backpressure_queue_size=0)
        with pytest.raises(ValidationError):
            StreamConfig(flush_interval_sec=-1)
        with pytest.raises(ValidationError):
            StreamConfig(flush_batch_size=0)

    def test_max_bounds(self) -> None:
        from pydantic import ValidationError

        from dxlink_scanner.config import StreamConfig

        with pytest.raises(ValidationError):
            StreamConfig(backpressure_queue_size=100001)
        with pytest.raises(ValidationError):
            StreamConfig(flush_batch_size=1000001)


class TestOutputsConfig:
    def test_data_dir_default(self) -> None:
        from dxlink_scanner.config import OutputsConfig

        oc = OutputsConfig()
        assert oc.data_dir == "data/events"
