"""Tests for the models."""

from datetime import UTC, datetime
from decimal import Decimal

from dxlink_scanner.models import Alert, OptionRow, RollingStats, StrikeInfo, TimeAndSaleEvent


def test_time_and_sale_event():
    event = TimeAndSaleEvent(
        symbol="SPY  260731C00450000:EQ",
        price=Decimal("1.23"),
        size=50,
        timestamp=datetime(2024, 7, 30, 16, 0, tzinfo=UTC),
    )
    assert event.symbol == "SPY  260731C00450000:EQ"
    assert event.price == Decimal("1.23")
    assert event.size == 50
    assert event.event_type == "TimeAndSale"


def test_strike_info():
    info = StrikeInfo(
        symbol="SPY  260731C00450000:EQ",
        strike=Decimal("450.00"),
        expiry="2024-07-31",
        option_type="call",
    )
    assert info.strike == Decimal("450.00")
    assert info.option_type == "call"


def test_option_row():
    row = OptionRow(
        streamer_symbol="SPY  260731C00450000:EQ",
        strike=Decimal("450.00"),
        expiry="2024-07-31",
        option_type="call",
        last=Decimal("1.23"),
        bid=Decimal("1.22"),
        ask=Decimal("1.24"),
        volume=500,
        open_interest=1000,
    )
    assert row.last == Decimal("1.23")
    assert row.volume == 500


def test_alert():
    alert = Alert(
        symbol="SPY  260731C00450000:EQ",
        price=Decimal("1.23"),
        size=500,
        timestamp_ms=1722355200000,
        median_size=50.0,
        ratio=10.0,
        rule_name="size_mult",
    )
    assert alert.rule_name == "size_mult"
    assert alert.ratio == 10.0


def test_rolling_stats_median_odd():
    stat = RollingStats(symbol="TEST")
    for s in [10, 20, 30]:
        stat.sizes.append(s)
    assert stat.median() == 20.0


def test_rolling_stats_median_even():
    stat = RollingStats(symbol="TEST")
    for s in [10, 20, 30, 40]:
        stat.sizes.append(s)
    assert stat.median() == 25.0


def test_rolling_stats_empty():
    stat = RollingStats(symbol="TEST")
    assert stat.median() == 0.0


def test_rolling_stats_mad():
    stat = RollingStats(symbol="TEST")
    for s in [10, 20, 30, 40, 50]:
        stat.sizes.append(s)
    assert stat.mad() > 0
