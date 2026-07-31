"""Stdout sink: outputs alerts as JSON lines to a stream."""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any, TextIO

import orjson

from dxlink_scanner.models import Alert


def _json_default(obj: Any) -> str:
    """JSON serializer for objects not serializable by default."""
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _alert_to_dict(alert: Alert) -> dict[str, Any]:
    """Convert an Alert to a JSON-serializable dict.

    Renames 'rule_name' to 'rule' for output compatibility.
    """
    return {
        "symbol": alert.symbol,
        "price": str(alert.price),
        "size": alert.size,
        "timestamp_ms": alert.timestamp_ms,
        "median_size": alert.median_size,
        "ratio": alert.ratio,
        "rule": alert.rule_name,
        "severity": alert.severity,
        "bid_price": str(alert.bid_price) if alert.bid_price else None,
        "ask_price": str(alert.ask_price) if alert.ask_price else None,
        "trade_type": alert.trade_type,
        "underlying_price": str(alert.underlying_price) if alert.underlying_price is not None else None,
    }


class StdoutSink:
    """Output alerts to stdout as JSON lines.

    Attributes:
        _stream: The output stream (defaults to sys.stdout).
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    async def send(self, alert: Alert) -> None:
        """Send an alert to the output stream as a JSON line."""
        payload = _alert_to_dict(alert)
        line = orjson.dumps(payload, default=_json_default)
        self._stream.write(line.decode("utf-8") + "\n")
        self._stream.flush()
