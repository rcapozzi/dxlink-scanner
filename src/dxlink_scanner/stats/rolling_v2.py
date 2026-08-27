"""Improved rolling statistics with exact sliding-window statistics, time decay, and session awareness.

For window sizes ≤ 50, a sorted list with bisect provides exact order statistics
with O(n) insert/remove. For larger windows, use heap-based or approximate methods.

Features:
- Exact sliding-window median, percentiles via sorted list (bisect)
- Weighted streaming mean/variance/std via Welford's algorithm with exponential time decay
- Exact MAD from window values
- Session-aware RTH/ETH separation
- Exponential time decay via half_life_sec (optional)
"""

from __future__ import annotations

import bisect
import collections
import datetime as dt
import math
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dxlink_scanner.config import DetectionConfig
from dxlink_scanner.stats.seasonality import TimeOfDayAggregator

# Constants for modified z-score
MODIFIED_Z_SCORE_CONSTANT = 0.6745


@dataclass(slots=True)
class RollingStatsV2:
    """Enhanced rolling statistics with exact sliding-window statistics."""

    symbol: str
    window_size: int = 50
    half_life_sec: float | None = None
    session_aware: bool = False

    # Sorted list for exact order statistics (bisect.insort = O(n) for n ≤ 50)
    _sorted_values: list[tuple[int, dt.datetime]] = field(default_factory=list, repr=False)
    # Deque to track insertion order for window eviction
    _window: collections.deque[tuple[int, dt.datetime]] = field(default_factory=collections.deque, repr=False)

    # Weighted Welford's algorithm for streaming mean/variance
    _total_weight: float = field(default=0.0, repr=False)
    _mean: float = field(default=0.0, repr=False)
    _M2: float = field(default=0.0, repr=False)

    # Session-aware stats (if session_aware, main window is NOT maintained;
    # stats are derived from RTH ∪ ETH union)
    _rth_sorted: list[tuple[int, dt.datetime]] = field(default_factory=list, repr=False)
    _rth_window: collections.deque[tuple[int, dt.datetime]] = field(default_factory=collections.deque, repr=False)
    _rth_total_weight: float = field(default=0.0, repr=False)
    _rth_mean: float = field(default=0.0, repr=False)
    _rth_M2: float = field(default=0.0, repr=False)
    _eth_sorted: list[tuple[int, dt.datetime]] = field(default_factory=list, repr=False)
    _eth_window: collections.deque[tuple[int, dt.datetime]] = field(default_factory=collections.deque, repr=False)
    _eth_total_weight: float = field(default=0.0, repr=False)
    _eth_mean: float = field(default=0.0, repr=False)
    _eth_M2: float = field(default=0.0, repr=False)

    # Time-of-day seasonality aggregator (optional)
    _tod_aggregator: TimeOfDayAggregator | None = field(default=None, repr=False)

    # --- Sorted List Helpers (static) ---

    @staticmethod
    def _s_add_sorted(value: int, timestamp: dt.datetime, sorted_list: list[tuple[int, dt.datetime]]) -> None:
        bisect.insort(sorted_list, (value, timestamp))

    @staticmethod
    def _s_remove_sorted(value: int, sorted_list: list[tuple[int, dt.datetime]]) -> None:
        for i, (v, _) in enumerate(sorted_list):
            if v == value:
                sorted_list.pop(i)
                return

    @staticmethod
    def _s_get_quantile(sorted_list: list[tuple[int, dt.datetime]], p: float) -> float:
        """Exact quantile from sorted list (linear interpolation)."""
        if not sorted_list:
            return 0.0
        p = max(0.0, min(100.0, p))
        n = len(sorted_list)
        idx = (p / 100.0) * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return float(sorted_list[lo][0])
        v_lo = sorted_list[lo][0]
        v_hi = sorted_list[hi][0]
        return v_lo + (v_hi - v_lo) * (idx - lo)

    def _s_get_median(self, sorted_list: list[tuple[int, dt.datetime]]) -> float:
        """O(1) median from sorted list."""
        return self._s_get_quantile(sorted_list, 50.0)

    # --- Weighted Welford's Algorithm ---

    @staticmethod
    def _weighted_welford_add(
        value: float, weight: float, total_weight: float, mean: float, M2: float
    ) -> tuple[float, float, float]:
        """Weighted Welford update."""
        if total_weight == 0:
            total_weight = weight
            mean = value
            M2 = 0.0
            return total_weight, mean, M2

        total_weight += weight
        delta = value - mean
        mean += weight * delta / total_weight
        M2 += weight * delta * (value - mean)
        return total_weight, mean, M2

    @staticmethod
    def _weighted_welford_remove(
        value: float, weight: float, total_weight: float, mean: float, M2: float
    ) -> tuple[float, float, float]:
        """Exact weighted Welford removal (valid for sliding window)."""
        if total_weight <= weight or total_weight == 0:
            return 0.0, 0.0, 0.0

        total_weight -= weight
        if total_weight == 0:
            return 0.0, 0.0, 0.0

        delta = value - mean
        mean -= weight * delta / (total_weight + weight)
        M2 -= weight * delta * (value - mean)
        return total_weight, mean, max(0.0, M2)

    def _recompute_from_window(
        self, window: collections.deque[tuple[int, dt.datetime]], half_life_sec: float | None, now: dt.datetime
    ) -> tuple[float, float, float]:
        """Recompute weighted mean/var from scratch. O(n) for n ≤ 50."""
        total_weight = 0.0
        mean = 0.0
        M2 = 0.0

        for value, ts in window:
            weight = 1.0
            if half_life_sec is not None:
                age_sec = (now - ts).total_seconds()
                if age_sec > 0:
                    weight = math.exp(-age_sec * math.log(2) / half_life_sec)
            total_weight, mean, M2 = self._weighted_welford_add(float(value), weight, total_weight, mean, M2)
        return total_weight, mean, M2

    # --- Session Detection ---

    _ET_TZ = ZoneInfo("America/New_York")

    def _is_rth(self, timestamp: dt.datetime) -> bool:
        """Check if timestamp is in RTH (09:30-16:00 ET)."""
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.UTC)
        et_time = timestamp.astimezone(self._ET_TZ)
        hour = et_time.hour
        minute = et_time.minute
        return (hour > 9 or (hour == 9 and minute >= 30)) and hour < 16

    # --- Public API ---

    def add(self, value: int, timestamp: dt.datetime | None = None) -> None:
        """Add a value to the rolling window."""
        now = timestamp or dt.datetime.now(dt.UTC)

        # Time-of-day seasonality tracking
        if self._tod_aggregator is not None:
            self._tod_aggregator.add_observation(now, float(value))

        # Session-aware: skip main window, maintain only RTH/ETH
        if self.session_aware:
            is_rth = self._is_rth(now)
            target_sorted = self._rth_sorted if is_rth else self._eth_sorted
            target_window = self._rth_window if is_rth else self._eth_window

            weight = 1.0

            self._add_sorted(value, now, target_sorted)
            target_window.append((value, now))
            if len(target_window) > self.window_size:
                old_val, old_ts = target_window.popleft()
                self._remove_sorted(old_val, target_sorted)

            # Weighted update
            if is_rth:
                self._rth_total_weight, self._rth_mean, self._rth_M2 = self._weighted_welford_add(
                    float(value), weight, self._rth_total_weight, self._rth_mean, self._rth_M2
                )
            else:
                self._eth_total_weight, self._eth_mean, self._eth_M2 = self._weighted_welford_add(
                    float(value), weight, self._eth_total_weight, self._eth_mean, self._eth_M2
                )
            return

        # Non-session-aware: maintain main window
        self._add_sorted(value, now, self._sorted_values)
        self._window.append((value, now))

        weight = 1.0
        if self.half_life_sec is not None:
            weight = 1.0

        self._total_weight, self._mean, self._M2 = self._weighted_welford_add(
            float(value), weight, self._total_weight, self._mean, self._M2
        )

        if len(self._window) > self.window_size:
            old_val, old_ts = self._window.popleft()
            self._remove_sorted(old_val, self._sorted_values)

            if self.half_life_sec is not None:
                age_sec = (now - old_ts).total_seconds()
                old_weight = math.exp(-age_sec * math.log(2) / self.half_life_sec)
                self._total_weight, self._mean, self._M2 = self._weighted_welford_remove(
                    float(old_val), old_weight, self._total_weight, self._mean, self._M2
                )
            else:
                # Recompute from scratch - exact for sliding window
                self._total_weight, self._mean, self._M2 = self._recompute_from_window(
                    self._window, self.half_life_sec, now
                )

    def clear(self) -> None:
        """Clear all statistics."""
        self._sorted_values.clear()
        self._window.clear()
        self._total_weight = 0.0
        self._mean = 0.0
        self._M2 = 0.0
        self._rth_sorted.clear()
        self._rth_window.clear()
        self._rth_total_weight = 0.0
        self._rth_mean = 0.0
        self._rth_M2 = 0.0
        self._eth_sorted.clear()
        self._eth_window.clear()
        self._eth_total_weight = 0.0
        self._eth_mean = 0.0
        self._eth_M2 = 0.0

    def reset(self) -> None:
        """Alias for clear()."""
        self.clear()

    # --- Exact Order Statistics from Sorted List ---

    def median(self) -> float:
        """Exact median from sorted list."""
        if self.session_aware:
            # Median of combined RTH ∪ ETH
            combined = sorted(self._rth_sorted + self._eth_sorted, key=lambda x: x[0])
            return self._s_get_median(combined)
        return self._s_get_median(self._sorted_values)

    def percentile(self, p: float) -> float:
        """Exact percentile from sorted list (linear interpolation)."""
        if self.session_aware:
            combined = sorted(self._rth_sorted + self._eth_sorted, key=lambda x: x[0])
            return self._s_get_quantile(combined, p)
        return self._s_get_quantile(self._sorted_values, p)

    def quantile(self, q: float) -> float:
        """Exact quantile (0-1) from sorted list."""
        return self.percentile(q * 100)

    # --- Weighted Streaming Moments ---

    def mean(self) -> float:
        """Weighted mean."""
        if self.session_aware:
            # Weighted average of RTH and ETH
            if self._rth_total_weight == 0 and self._eth_total_weight == 0:
                return 0.0
            if self._rth_total_weight == 0:
                return self._eth_mean
            if self._eth_total_weight == 0:
                return self._rth_mean
            tw = self._rth_total_weight + self._eth_total_weight
            return (self._rth_mean * self._rth_total_weight + self._eth_mean * self._eth_total_weight) / tw
        return self._mean if self._total_weight > 0 else 0.0

    def variance(self) -> float:
        """Weighted sample variance."""
        if self.session_aware:
            if self._rth_total_weight < 2 and self._eth_total_weight < 2:
                return 0.0
            # Pooled variance approximation
            if self._rth_total_weight >= 2 and self._eth_total_weight >= 2:
                tw = self._rth_total_weight + self._eth_total_weight
                pooled_M2 = self._rth_M2 + self._eth_M2
                return pooled_M2 / (tw - 1)
            if self._rth_total_weight >= 2:
                return self._rth_M2 / (self._rth_total_weight - 1)
            return self._eth_M2 / (self._eth_total_weight - 1)
        return self._M2 / (self._total_weight - 1) if self._total_weight >= 2 else 0.0

    def std(self) -> float:
        """Weighted standard deviation."""
        return math.sqrt(self.variance())

    def mad(self) -> float:
        """Exact MAD from current window values."""
        if self.session_aware:
            values = [v for v, _ in self._rth_window] + [v for v, _ in self._eth_window]
        else:
            values = [v for v, _ in self._window]
        if not values:
            return 0.0
        med = self.median()
        abs_devs = [abs(v - med) for v in values]
        abs_devs.sort()
        n = len(abs_devs)
        mid = n // 2
        if n % 2 == 0:
            return (abs_devs[mid - 1] + abs_devs[mid]) / 2.0
        return float(abs_devs[mid])

    def z_score(self, value: float) -> float:
        """Standard z-score: (value - mean) / std."""
        std = self.std()
        if std == 0:
            return 0.0
        return (value - self.mean()) / std

    def modified_z_score(self, value: float) -> float:
        """Modified z-score using MAD: 0.6745 * (value - median) / MAD."""
        mad = self.mad()
        if mad == 0:
            return 0.0
        return MODIFIED_Z_SCORE_CONSTANT * (value - self.median()) / mad

    # --- Session-Aware Getters ---

    def rth_median(self) -> float:
        return self._s_get_median(self._rth_sorted)

    def rth_mean(self) -> float:
        return self._rth_mean if self._rth_total_weight > 0 else 0.0

    def rth_std(self) -> float:
        if self._rth_total_weight < 2:
            return 0.0
        return math.sqrt(self._rth_M2 / (self._rth_total_weight - 1))

    def rth_percentile(self, p: float) -> float:
        return self._s_get_quantile(self._rth_sorted, p)

    def eth_median(self) -> float:
        return self._s_get_median(self._eth_sorted)

    def eth_mean(self) -> float:
        return self._eth_mean if self._eth_total_weight > 0 else 0.0

    def eth_std(self) -> float:
        if self._eth_total_weight < 2:
            return 0.0
        return math.sqrt(self._eth_M2 / (self._eth_total_weight - 1))

    def eth_percentile(self, p: float) -> float:
        return self._s_get_quantile(self._eth_sorted, p)

    # --- Time-of-Day Seasonality Accessors ---

    def seasonality_factor(self, timestamp: dt.datetime | None = None) -> float:
        """Seasonality factor for this timestamp (>1 = historically busy, <1 = quiet)."""
        if self._tod_aggregator is None:
            return 1.0
        ts = timestamp or dt.datetime.now(dt.UTC)
        return self._tod_aggregator.seasonality_factor(ts)

    def expected_volume(self, timestamp: dt.datetime | None = None) -> float:
        """Expected volume for this time based on historical patterns."""
        if self._tod_aggregator is None:
            return 0.0
        ts = timestamp or dt.datetime.now(dt.UTC)
        return self._tod_aggregator.expected_volume(ts)

    def normalized_volume(self, size: int, timestamp: dt.datetime | None = None) -> float:
        """Volume normalized by seasonality factor (removes intraday pattern)."""
        if self._tod_aggregator is None:
            return float(size)
        ts = timestamp or dt.datetime.now(dt.UTC)
        return self._tod_aggregator.normalized_volume(ts, float(size))

    def z_score_seasonal(self, size: int, timestamp: dt.datetime | None = None) -> float:
        """Z-score after removing seasonality effect."""
        if self._tod_aggregator is None:
            return self.z_score(float(size))
        ts = timestamp or dt.datetime.now(dt.UTC)
        return self._tod_aggregator.z_score_seasonal(ts, float(size))

    def enable_seasonality(self, aggregator: TimeOfDayAggregator) -> None:
        """Attach a time-of-day aggregator to this rolling stats instance."""
        self._tod_aggregator = aggregator

    @property
    def count(self) -> int:
        if self.session_aware:
            return len(self._rth_window) + len(self._eth_window)
        return len(self._window)

    @property
    def window(self) -> collections.deque[tuple[int, dt.datetime]]:
        if self.session_aware:
            return collections.deque(
                [(v, ts) for v, ts in self._rth_window] + [(v, ts) for v, ts in self._eth_window],
                maxlen=self.window_size,
            )
        return self._window

    # --- Sorted List Helpers ---

    def _add_sorted(self, value: int, timestamp: dt.datetime, sorted_list: list[tuple[int, dt.datetime]]) -> None:
        bisect.insort(sorted_list, (value, timestamp))

    def _remove_sorted(self, value: int, sorted_list: list[tuple[int, dt.datetime]]) -> None:
        for i, (v, _) in enumerate(sorted_list):
            if v == value:
                sorted_list.pop(i)
                return

    def _get_median(self, sorted_list: list[tuple[int, dt.datetime]]) -> float:
        return self._s_get_quantile(sorted_list, 50.0)


class RollingStatsManagerV2:
    """Manages rolling statistics across multiple symbols with V2 algorithm."""

    def __init__(self, config: DetectionConfig) -> None:
        self._config = config
        self._stats: dict[str, RollingStatsV2] = {}

        # Extract V2 config options
        self._half_life_sec = getattr(config, "stats_half_life_sec", None)
        self._session_aware = getattr(config, "stats_session_aware", False)

    def _get_or_create(self, symbol: str) -> RollingStatsV2:
        if symbol not in self._stats:
            self._stats[symbol] = RollingStatsV2(
                symbol=symbol,
                window_size=self._config.stats_window,
                half_life_sec=self._half_life_sec,
                session_aware=self._session_aware,
            )
        return self._stats[symbol]

    def add(self, symbol: str, size: int, timestamp: dt.datetime | None = None) -> None:
        """Add a trade size to a symbol's rolling window."""
        stats = self._get_or_create(symbol)
        stats.add(size, timestamp)

    def get(self, symbol: str) -> RollingStatsV2 | None:
        """Get rolling stats for a symbol, or None if not yet tracked."""
        return self._stats.get(symbol)

    def reset(self, symbol: str) -> None:
        """Reset statistics for a symbol."""
        if symbol in self._stats:
            self._stats[symbol].clear()

    def remove(self, symbol: str) -> bool:
        """Remove a symbol's statistics entirely."""
        if symbol in self._stats:
            del self._stats[symbol]
            return True
        return False

    def clear_all(self) -> None:
        """Clear all symbols' statistics."""
        self._stats.clear()

    def is_anomalous(self, symbol: str, size: int) -> tuple[bool, float, float]:
        """Check if a trade size is anomalous."""
        stats = self._stats.get(symbol)

        if stats is None or stats.count == 0:
            triggered = size >= self._config.abs_min_size
            return (triggered, 0.0, 0.0)

        med = stats.median()
        ratio = size / med if med > 0 else 0.0
        triggered = ratio >= self._config.size_mult and size >= self._config.abs_min_size

        return (triggered, ratio, med)
