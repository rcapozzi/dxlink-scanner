"""Tests for the stdout sink."""

import io
from decimal import Decimal

import orjson

from dxlink_scanner.models import Alert
from dxlink_scanner.sinks.stdout_sink import StdoutSink


def test_stdout_sink_outputs_jsonl():
    """Test that StdoutSink outputs valid JSONL."""
    stream = io.StringIO()
    sink = StdoutSink(stream=stream)

    alert = Alert(
        symbol="SPY  260731C00450000:EQ",
        price=Decimal("1.23"),
        size=500,
        timestamp_ms=1722355200000,
        rule_name="size_mult",
    )

    import asyncio

    asyncio.run(sink.send(alert))

    output = stream.getvalue().strip()
    assert output
    data = orjson.loads(output)
    assert data["symbol"] == "SPY  260731C00450000:EQ"
    assert data["size"] == 500
    assert data["price"] == "1.23"
    assert data["rule"] == "size_mult"
    assert data["underlying_price"] is None
