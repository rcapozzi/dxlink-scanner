"""Time-of-day seasonality aggregation for trading volume data.

Provides intraday volume seasonality modeling: how trade sizes typically
distribute across the trading day. Used to normalize real-time observations
against expected intraday patterns (e.g., higher volume at open/close).
"""

from __future__ import annotations

import collections
import datetime as dt
import math
from zoneinfo import ZoneInfo

# Default bin edges: every 15 minutes from 7:00 ET to 21:00 ET
# Covers pre-market (7-9:30), RTH (9:30-16), after-hours (16-21)
_DEFAULT_BIN_EDGES: list[int] = [
    7 * 60,    # 07:00
    9 * 60 + 30,  # 09:30 (RTH open)
    10 * 60,   # 10:00
    11 * 60,   # 11:00
    12 * 60,   # 12:00
    13 * 60,   # 13:00
    14 * 60,   # 14:00
    15 * 60,   # 15:00
    16 * 60,   # 16:00 (RTH close)
    17 * 60,   # 17:00
    18 * 60,   # 18:00
    19 * 60,   # 19:00
    20 * 60,   # 20:00
    21 * 60,   # 21:00
]
_ET_TZ = ZoneInfo("America/New_York")


def _et_minute(timestamp: dt.datetime) -> int:
    """Convert a datetime to ET minutes-since-midnight."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.UTC)
    et_time = timestamp.astimezone(_ET_TZ)
    return et_time.hour * 60 + et_time.minute


def _find_bin(et_minute_val: int, bin_edges: list[int]) -> int:
    """Find the bin index for an ET minute value using bisect."""
    lo, hi = 0, len(bin_edges)
    while lo < hi:
        mid = (lo + hi) // 2
        if bin_edges[mid] <= et_minute_val:
            lo = mid + 1
        else:
            hi = mid
    return lo  # Insertion index = bin index


class TimeOfDayAggregator:
    """Aggregates trade data into intraday time-of-day bins.

    Maintains per-bin count, sum, sum-of-squares, and a ring buffer
    for recent values. Provides normalization factors to correct
    for intraday seasonality in volume and trade sizes.
    """

    def __init__(
        self,
        bin_edges: list[int] | None = None,
        window_days: int = 5,
        min_observations_per_bin: int = 10,
    ) -> None:
        self.bin_edges: list[int] = bin_edges or list(_DEFAULT_BIN_EDGES)
        self.window_days: int = window_days
        self.min_observations_per_bin: int = min_observations_per_bin

        # Per-bin statistics (rolling window of recent days)
        # Each bin stores a deque of (date, count, sum, sum_sq) tuples
        self._bin_stats: dict[int, collections.deque] = {
            edge: collections.deque(maxlen=window_days)
            for edge in self.bin_edges
        }
        # Global stats
        self._global_sum: float = 0.0
        self._global_count: int = 0

    def _get_bin_index(self, et_minute_val: int) -> int:
        """Get the bin index for an ET minute value."""
        return _find_bin(et_minute_val, self.bin_edges)

    def add_observation(
        self, timestamp: dt.datetime, value: float
    ) -> None:
        """Add a single volume/price observation to the appropriate time bin."""
        et_min = _et_minute(timestamp)
        bin_idx = self._get_bin_index(et_min)
        if bin_idx >= len(self.bin_edges):
            return  # Outside trading hours

        bin_edge = self.bin_edges[bin_idx]
        # Use date as the rolling-window key
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.UTC)
        et_date = timestamp.astimezone(_ET_TZ).date()

        stats_deque = self._bin_stats[bin_edge]

        # Find or create entry for today
        today_entry: dict[str, object] | None = None
        for entry in stats_deque:
            if entry["date"] == et_date:
                today_entry = entry
                break

        if today_entry is None:
            today_entry = {
                "date": et_date,
                "count": 0,
                "sum": 0.0,
                "sum_sq": 0.0,
            }
            stats_deque.append(today_entry)

        today_entry["count"] = today_entry["count"] + 1  # type: ignore[operator]
        today_entry["sum"] = today_entry["sum"] + value  # type: ignore[operator]
        today_entry["sum_sq"] = today_entry["sum_sq"] + value * value  # type: ignore[operator]

        # Update global stats
        self._global_sum += value
        self._global_count += 1

    @property
    def bin_edges_readonly(self) -> list[int]:
        """Return a copy of bin edges."""
        return list(self.bin_edges)

    def _bin_mean(self, bin_edge: int) -> float:
        """Compute the mean volume for a given bin across the rolling window."""
        stats_deque = self._bin_stats[bin_edge]
        if not stats_deque:
            return 0.0
        total_count = sum(e["count"] for e in stats_deque)
        total_sum = sum(e["sum"] for e in stats_deque)
        return total_sum / total_count if total_count > 0 else 0.0

    def _global_mean(self) -> float:
        """Compute the global mean across all bins and observations."""
        if self._global_count == 0:
            return 1.0  # Avoid division by zero
        return self._global_sum / self._global_count

    def seasonality_factor(self, timestamp: dt.datetime) -> float:
        """Compute the seasonality factor for a given timestamp.

        Returns the ratio: bin_mean / global_mean.
        A factor > 1.0 means this time period historically has higher volume.

        If insufficient data in the bin, returns 1.0 (neutral).
        """
        et_min = _et_minute(timestamp)
        bin_idx = self._get_bin_index(et_min)
        if bin_idx >= len(self.bin_edges):
            return 1.0

        bin_edge = self.bin_edges[bin_idx]
        bin_mean = self._bin_mean(bin_edge)
        global_mean = self._global_mean()

        # Check minimum observations
        stats_deque = self._bin_stats[bin_edge]
        total_count = sum(e["count"] for e in stats_deque)
        if total_count < self.min_observations_per_bin:
            return 1.0

        if global_mean == 0:
            return 1.0
        return bin_mean / global_mean

    def expected_volume(self, timestamp: dt.datetime) -> float:
        """Expected volume for a given timestamp based on historical patterns."""
        et_min = _et_minute(timestamp)
        bin_idx = self._get_bin_index(et_min)
        if bin_idx >= len(self.bin_edges):
            return 0.0

        bin_edge = self.bin_edges[bin_idx]
        return self._bin_mean(bin_edge)

    def normalized_volume(self, timestamp: dt.datetime, volume: float) -> float:
        """Normalize a volume observation by removing the seasonality component.

        Returns: volume / seasonality_factor.
        Values > 1.0 indicate unexpectedly high volume for this time of day.
        """
        factor = self.seasonality_factor(timestamp)
        if factor <= 0:
            return float(volume)
        return float(volume) / factor

    def expected_std(self, timestamp: dt.datetime) -> float:
        """Compute the standard deviation for a given time bin."""
        et_min = _et_minute(timestamp)
        bin_idx = self._get_bin_index(et_min)
        if bin_idx >= len(self.bin_edges):
            return 0.0

        bin_edge = self.bin_edges[bin_idx]
        stats_deque = self._bin_stats[bin_edge]
        if not stats_deque:
            return 0.0

        total_count = sum(e["count"] for e in stats_deque)
        if total_count < self.min_observations_per_bin or total_count < 2:
            return 0.0

        total_sum = sum(e["sum"] for e in stats_deque)
        total_sum_sq = sum(e["sum_sq"] for e in stats_deque)
        mean = total_sum / total_count
        variance = (total_sum_sq / total_count) - (mean * mean)
        return math.sqrt(max(0.0, variance))

    def z_score_seasonal(self, timestamp: dt.datetime, volume: float) -> float:
        """Z-score of volume after removing seasonality.

        Returns: (normalized_volume - expected_volume) / expected_std
        """
        expected = self.expected_volume(timestamp)
        std = self.expected_std(timestamp)
        if std == 0:
            return 0.0
        normalized = self.normalized_volume(timestamp, volume)
        return (normalized - expected) / std

    def to_dict(self) -> dict:
        """Serialize to dict for persistence."""
        return {
            "bin_edges": self.bin_edges,
            "window_days": self.window_days,
            "min_observations_per_bin": self.min_observations_per_bin,
            "bin_stats": {
                str(edge): list(stats_deque)
                for edge, stats_deque in self._bin_stats.items()
            },
            "global_sum": self._global_sum,
            "global_count": self._global_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TimeOfDayAggregator:
        """Deserialize from dict."""
        agg = cls(
            bin_edges=data.get("bin_edges", list(_DEFAULT_BIN_EDGES)),
            window_days=data.get("window_days", 5),
            min_observations_per_bin=data.get("min_observations_per_bin", 10),
        )
        agg._global_sum = data.get("global_sum", 0.0)
        agg._global_count = data.get("global_count", 0)
        bin_stats_raw = data.get("bin_stats", {})
        for edge in agg.bin_edges:
            deque = collections.deque(maxlen=agg.window_days)
            for entry in bin_stats_raw.get(str(edge), []):
                deque.append(entry)
            agg._bin_stats[edge] = deque
        return agg

    def clear(self) -> None:
        """Reset all bin statistics."""
        self._bin_stats = {
            edge: collections.deque(maxlen=self.window_days)
            for edge in self.bin_edges
        }
        self._global_sum = 0.0
        self._global_count = 0
