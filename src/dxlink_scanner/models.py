"""Consolidated data models for merged DXLink event types."""

from __future__ import annotations

import collections
import datetime as dt
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Literal

from tastytrade.dxfeed import Quote as DXQuote
from tastytrade.dxfeed import TimeAndSale as DXTimeAndSale


def _to_epoch_ms(ts: object) -> int | None:
    """Convert a DXLink timestamp to epoch milliseconds."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        try:
            parsed = dt.datetime.fromisoformat(ts)
            return int(parsed.timestamp() * 1000)
        except (ValueError, OSError):
            return None
    if isinstance(ts, dt.datetime):
        return int(ts.timestamp() * 1000)
    return None


def _parse_dt(ts: object) -> dt.datetime | None:
    """Convert a DXLink timestamp to datetime."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts / 1000, tz=dt.UTC)
    if isinstance(ts, str):
        try:
            return dt.datetime.fromisoformat(ts)
        except (ValueError, OSError):
            return None
    if isinstance(ts, dt.datetime):
        return ts
    return None


@dataclass(slots=True)
class ConsolidatedSnapshot:
    """Latest merged state for a single symbol.

    Updated incrementally as Quote and TimeAndSale events arrive.
    Stored in a dict keyed by symbol in real-time processing.
    """

    symbol: str
    underlying_symbol: str
    updated_at: dt.datetime
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    last_trade_price: Decimal | None = None
    last_trade_size: int | None = None
    last_trade_time: int | None = None  # epoch ms
    last_trade_type: str | None = None

    # Derived
    mid_price: Decimal | None = None
    spread: Decimal | None = None
    spread_bps: float | None = None
    trade_vs_mid: Decimal | None = None
    evict_at: int | None = None


@dataclass(frozen=True, slots=True)
class ConsolidatedEvent:
    """Immutable representation of an incoming DXLink message."""

    event_id: int
    received_at: dt.datetime
    source_type: Literal["QUOTE", "TIME_AND_SALE"]
    symbol: str
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    last_trade_price: Decimal | None = None
    last_trade_size: int | None = None
    last_trade_time: int | None = None  # epoch ms
    last_trade_type: str | None = None
    event_time_ms: int | None = None


def normalize_quote(q: DXQuote, event_id: int) -> ConsolidatedEvent:
    """Convert a DXLink Quote to ConsolidatedEvent."""
    return ConsolidatedEvent(
        event_id=event_id,
        received_at=dt.datetime.now(dt.UTC),
        source_type="QUOTE",
        symbol=q.event_symbol,
        bid_price=Decimal(str(q.bid_price)) if q.bid_price else None,
        ask_price=Decimal(str(q.ask_price)) if q.ask_price else None,
        event_time_ms=_to_epoch_ms(q.event_time),
    )


def normalize_timeandsale(tas: DXTimeAndSale, event_id: int) -> ConsolidatedEvent:
    """Convert a DXLink TimeAndSale to ConsolidatedEvent."""
    return ConsolidatedEvent(
        event_id=event_id,
        received_at=dt.datetime.now(dt.UTC),
        source_type="TIME_AND_SALE",
        symbol=tas.event_symbol,
        last_trade_price=Decimal(str(tas.price)) if tas.price else None,
        last_trade_size=int(tas.size) if tas.size else None,
        last_trade_type=tas.type if hasattr(tas, "type") and tas.type else None,
        event_time_ms=_to_epoch_ms(tas.time),
    )


def merge_into_snapshot(snap: ConsolidatedSnapshot, event: ConsolidatedEvent) -> ConsolidatedSnapshot:
    """Merge a ConsolidatedEvent into an existing snapshot."""
    now = dt.datetime.now(dt.UTC)

    if event.source_type == "QUOTE":
        snap.bid_price = event.bid_price
        snap.ask_price = event.ask_price
    elif event.source_type == "TIME_AND_SALE":
        snap.last_trade_price = event.last_trade_price
        snap.last_trade_size = event.last_trade_size
        snap.last_trade_time = _to_epoch_ms(event.last_trade_time) if event.last_trade_time else None
        snap.last_trade_type = event.last_trade_type

    snap.updated_at = now

    if snap.bid_price is not None and snap.ask_price is not None:
        snap.mid_price = (snap.bid_price + snap.ask_price) / 2
        snap.spread = snap.ask_price - snap.bid_price
        if snap.mid_price and snap.mid_price != 0:
            snap.spread_bps = float(snap.spread / snap.mid_price * 10000)
        else:
            snap.spread_bps = None
    else:
        snap.mid_price = None
        snap.spread = None
        snap.spread_bps = None

    if snap.last_trade_price is not None and snap.mid_price is not None:
        snap.trade_vs_mid = snap.last_trade_price - snap.mid_price
    else:
        snap.trade_vs_mid = None

    return snap


def snapshot_to_dict(snap: ConsolidatedSnapshot) -> dict[str, object]:
    """Convert a snapshot to a plain dict for parquet serialization."""
    d: dict[str, object] = asdict(snap)
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = str(v)
        elif isinstance(v, dt.datetime):
            d[k] = v.isoformat()
    return d


# --- Existing models (preserved) ---


@dataclass(frozen=True, slots=True)
class TimeAndSaleEvent:
    symbol: str
    price: Decimal
    size: int
    timestamp: dt.datetime
    event_type: Literal["TimeAndSale"] = "TimeAndSale"
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    trade_type: str | None = None


@dataclass(frozen=True, slots=True)
class StrikeInfo:
    symbol: str
    strike: Decimal
    expiry: str
    option_type: str


@dataclass(frozen=True, slots=True)
class OptionRow:
    streamer_symbol: str
    strike: Decimal
    expiry: str
    option_type: str
    last: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None


@dataclass(frozen=True, slots=True)
class Alert:
    """Represents a triggered alert from the rule engine.

    Attributes:
        symbol: The streamer symbol that triggered the alert.
        price: Trade price at trigger time.
        size: Trade size at trigger time.
        timestamp_ms: Epoch milliseconds when the trade was received.
        rule_name: Name of the rule that triggered.
        severity: Alert severity level (info, low, medium, high, critical).
        underlying_price: Most recent underlying price at trigger time,
            derived from Quote mid_price (bid+ask)/2 on the underlying.
    """

    symbol: str
    price: Decimal
    size: int
    timestamp_ms: int
    rule_name: str
    severity: str = "high"
    underlying_price: float | None = None


@dataclass(slots=True)
class RollingStats:
    symbol: str
    sizes: collections.deque[int] | None = None

    def __post_init__(self) -> None:
        if self.sizes is None:
            self.sizes = collections.deque(maxlen=50)

    def median(self) -> float:
        """Return the median of recent sizes, or 0.0 if empty."""
        if not self.sizes:
            return 0.0
        sorted_sizes = sorted(self.sizes)
        n = len(sorted_sizes)
        mid = n // 2
        if n % 2 == 0:
            return float((sorted_sizes[mid - 1] + sorted_sizes[mid]) / 2)
        return float(sorted_sizes[mid])

    def mad(self) -> float:
        """Return the Median Absolute Deviation of recent sizes."""
        if not self.sizes:
            return 0.0
        med = self.median()
        abs_devs = sorted(abs(s - med) for s in self.sizes)
        n = len(abs_devs)
        mid = n // 2
        if n % 2 == 0:
            return float((abs_devs[mid - 1] + abs_devs[mid]) / 2)
        return float(abs_devs[mid])
