"""Tests for the CLI adapter functions."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

from dxlink_scanner.cli import _timeandsale_to_event


def _make_tas(**kwargs):
    """Create a mock TimeAndSale object for testing."""

    defaults = {
        "event_symbol": ".SPY260731C500",
        "price": 1.23,
        "size": 50,
        "event_time": datetime(2026, 7, 31, 15, 30, 0, tzinfo=UTC),
        "bid_price": 1.20,
        "ask_price": 1.25,
        "type": "REGULAR",
    }
    defaults.update(kwargs)
    return Mock(**defaults)


def test_timeandsale_to_event_basic():
    """Test conversion of SDK TimeAndSale to scanner TimeAndSaleEvent."""
    tas = _make_tas()
    event = _timeandsale_to_event(tas)

    assert event.symbol == ".SPY260731C500"
    assert event.price == Decimal("1.23")
    assert event.size == 50
    assert event.timestamp == datetime(2026, 7, 31, 15, 30, 0, tzinfo=UTC)
    assert event.bid_price == Decimal("1.2")
    assert event.ask_price == Decimal("1.25")
    assert event.trade_type == "REGULAR"


def test_timeandsale_to_event_with_none_values():
    """Test conversion handles None/empty values from SDK."""
    tas = _make_tas(
        price=None,
        size=None,
        bid_price=None,
        ask_price=None,
        type=None,
    )
    event = _timeandsale_to_event(tas)

    assert event.size == 0
    assert event.price == Decimal("0")
    assert event.bid_price is None
    assert event.ask_price is None
    assert event.trade_type is None


def test_timeandsale_to_event_string_timestamp():
    """Test conversion handles string timestamp from SDK."""
    tas = _make_tas(
        event_time="2026-07-31T15:30:00+00:00",
    )
    event = _timeandsale_to_event(tas)
    assert event.timestamp == datetime(2026, 7, 31, 15, 30, 0, tzinfo=UTC)


def test_timeandsale_to_event_int_timestamp():
    """Test conversion handles integer millisecond timestamp from SDK."""
    tas = _make_tas(
        event_time=1722414600000,  # 2024-07-31 08:30:00 UTC in ms
    )
    event = _timeandsale_to_event(tas)
    assert event.timestamp == datetime(2024, 7, 31, 8, 30, 0, tzinfo=UTC)
