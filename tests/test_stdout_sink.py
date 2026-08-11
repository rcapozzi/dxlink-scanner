"""Tests for the stdout sink JSON serialization."""

import io
from decimal import Decimal

import orjson

from dxlink_scanner.models import Alert
from dxlink_scanner.sinks.stdout_sink import StdoutSink, _alert_to_dict, _json_default


def test_json_default_decimal():
    result = _json_default(Decimal("1.23"))
    assert result == "1.23"


def test_json_default_fallback():
    import pytest as pytest_mod

    with pytest_mod.raises(TypeError, match="int"):
        _json_default(42)


def test_alert_to_dict():
    alert = Alert(
        symbol="SPY  260731C00450000:EQ",
        price=Decimal("1.23"),
        size=500,
        timestamp_ms=1722355200000,
        rule_name="size_mult",
    )
    d = _alert_to_dict(alert)
    assert d["symbol"] == "SPY  260731C00450000:EQ"
    assert d["price"] == "1.23"
    assert d["size"] == 500
    assert d["rule"] == "size_mult"
    assert d["timestamp_ms"] == 1722355200000  # 2024-07-30T16:00:00 UTC

    assert d["severity"] == "high"
    assert d["underlying_price"] is None


def test_stdout_sink_minimal_alert():
    stream = io.StringIO()
    sink = StdoutSink(stream=stream)

    alert = Alert(
        symbol="TEST",
        price=Decimal("0.01"),
        size=10,
        timestamp_ms=1704067200000,  # 2024-01-01T00:00:00 UTC
        rule_name="abs_min",
    )

    import asyncio

    asyncio.run(sink.send(alert))
    output = stream.getvalue().strip()
    data = orjson.loads(output)
    assert data["rule"] == "abs_min"
