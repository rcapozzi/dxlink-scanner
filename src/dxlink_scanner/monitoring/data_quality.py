"""Data quality monitors for the DXLink scanner.

Provides gap detection in event streams, schema drift detection,
and outlier detection in model parameters.

Classes:
    DataQualityMonitor — Detects gaps, schema drift, and data anomalies
    ModelParamTracker — Tracks model parameters for outlier/drift detection
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq  # type: ignore[import-untyped]

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class GapReport:
    """Report of detected gaps in event timestamps."""
    symbol: str
    gap_start: dt.datetime
    gap_end: dt.datetime
    gap_duration_sec: float
    event_count_before: int
    event_count_after: int


@dataclass
class SchemaDriftReport:
    """Report of schema drift between expected and actual parquet files."""
    file_path: str
    expected_fields: list[str]
    actual_fields: list[str]
    missing_fields: list[str]
    unexpected_fields: list[str]


@dataclass
class ModelParamOutlier:
    """Report of an outlier in model parameters."""
    symbol: str
    parameter: str
    current_value: float
    historical_mean: float
    historical_std: float
    z_score: float


class DataQualityMonitor:
    """Detects data quality issues in the event pipeline.

    Monitors:
    - Timestamp gaps in the consolidated event stream (gap detection)
    - Schema drift in parquet files (missing/unexpected columns)
    - Missing data duration thresholds

    Attributes:
        max_gap_sec: Maximum acceptable gap between consecutive events (default: 60s).
        max_missing_duration: Maximum acceptable duration of missing data before alert (default: 300s).
    """

    def __init__(
        self,
        max_gap_sec: float = 60.0,
        max_missing_duration: float = 300.0,
    ) -> None:
        self.max_gap_sec = max_gap_sec
        self.max_missing_duration = max_missing_duration
        self._last_event_time: dict[str, dt.datetime] = {}
        self._event_counts: dict[str, int] = {}
        self._gaps: list[GapReport] = []

    def record_event(self, symbol: str, timestamp: dt.datetime) -> GapReport | None:
        """Record an event timestamp; return a GapReport if a gap was detected.

        Args:
            symbol: The symbol for this event.
            timestamp: The event timestamp.

        Returns:
            GapReport if a gap exceeding max_gap_sec was detected, else None.
        """
        prev = self._last_event_time.get(symbol)
        self._last_event_time[symbol] = timestamp
        self._event_counts[symbol] = self._event_counts.get(symbol, 0) + 1

        if prev is not None:
            gap = (timestamp - prev).total_seconds()
            if gap > self.max_gap_sec:
                report = GapReport(
                    symbol=symbol,
                    gap_start=prev,
                    gap_end=timestamp,
                    gap_duration_sec=gap,
                    event_count_before=self._event_counts.get(symbol, 0) - 1,
                    event_count_after=1,
                )
                self._gaps.append(report)
                logger.warning(
                    "Gap detected for %s: %.1fs (last=%s, current=%s)",
                    symbol, gap, prev.isoformat(), timestamp.isoformat(),
                )
                return report
        return None

    def detect_gaps_in_parquet(
        self, parquet_path: str | Path, timestamp_col: str = "event_time_ms"
    ) -> list[GapReport]:
        """Scan a parquet file for timestamp gaps.

        Args:
            parquet_path: Path to the parquet file.
            timestamp_col: Column name containing epoch-ms timestamps.

        Returns:
            List of GapReport objects for detected gaps.
        """
        path = Path(parquet_path)
        if not path.exists():
            return []

        table = pq.read_table(str(path), columns=["symbol", timestamp_col])
        if table.num_rows == 0:
            return []

        symbols = table.column("symbol").to_pylist()
        timestamps = table.column(timestamp_col).to_pylist()

        gaps: list[GapReport] = []
        per_symbol: dict[str, list[int]] = {}
        for sym, ts in zip(symbols, timestamps, strict=True):
            if sym is None or ts is None:
                continue
            per_symbol.setdefault(sym, []).append(int(ts))

        for sym, ts_list in per_symbol.items():
            ts_list.sort()
            for i in range(1, len(ts_list)):
                gap_sec = (ts_list[i] - ts_list[i - 1]) / 1000.0
                if gap_sec > self.max_gap_sec:
                    gaps.append(GapReport(
                        symbol=sym,
                        gap_start=dt.datetime.fromtimestamp(ts_list[i - 1] / 1000, tz=dt.UTC),
                        gap_end=dt.datetime.fromtimestamp(ts_list[i] / 1000, tz=dt.UTC),
                        gap_duration_sec=gap_sec,
                        event_count_before=i,
                        event_count_after=len(ts_list) - i,
                    ))

        return gaps

    def check_schema_drift(
        self, parquet_path: str | Path, expected_fields: list[str]
    ) -> SchemaDriftReport | None:
        """Check a parquet file for schema drift.

        Args:
            parquet_path: Path to the parquet file.
            expected_fields: Expected column names.

        Returns:
            SchemaDriftReport if drift detected, else None.
        """
        path = Path(parquet_path)
        if not path.exists():
            return None

        schema = pq.read_schema(str(path))
        actual_fields = schema.names
        missing = [f for f in expected_fields if f not in actual_fields]
        unexpected = [f for f in actual_fields if f not in expected_fields]

        if missing or unexpected:
            report = SchemaDriftReport(
                file_path=str(path),
                expected_fields=expected_fields,
                actual_fields=list(actual_fields),
                missing_fields=missing,
                unexpected_fields=unexpected,
            )
            if missing:
                logger.warning("Missing fields in %s: %s", path.name, missing)
            if unexpected:
                logger.warning("Unexpected fields in %s: %s", path.name, unexpected)
            return report
        return None

    @property
    def recent_gaps(self) -> list[GapReport]:
        """Gaps detected in the last 100 events."""
        return self._gaps[-100:]

    def reset(self) -> None:
        """Clear all recorded state."""
        self._last_event_time.clear()
        self._event_counts.clear()
        self._gaps.clear()


class ModelParamTracker:
    """Tracks model parameters over time for outlier/drift detection.

    Records parameter values (alpha, beta, mu, etc.) per symbol and
    detects when current values deviate significantly from historical norms.

    Attributes:
        z_threshold: Z-score threshold for outlier detection (default: 3.0).
        history_window: Number of recent values to retain per parameter (default: 100).
    """

    def __init__(
        self,
        z_threshold: float = 3.0,
        history_window: int = 100,
    ) -> None:
        self.z_threshold = z_threshold
        self.history_window = history_window
        self._history: dict[str, dict[str, list[float]]] = {}

    def record(
        self, symbol: str, params: dict[str, float]
    ) -> list[ModelParamOutlier]:
        """Record parameter values for a symbol; return outliers.

        Args:
            symbol: The symbol identifier.
            params: Dict of parameter_name → value.

        Returns:
            List of ModelParamOutlier for parameters that are outliers.
        """
        if symbol not in self._history:
            self._history[symbol] = {}

        outliers: list[ModelParamOutlier] = []
        for param_name, value in params.items():
            if param_name not in self._history[symbol]:
                self._history[symbol][param_name] = []

            history = self._history[symbol][param_name]
            # Check for outlier against historical values (need at least 5 for std)
            if len(history) >= 5:
                mean = statistics.mean(history)
                std = statistics.pstdev(history)
                if std > 0:
                    z_score = abs(value - mean) / std
                    if z_score > self.z_threshold:
                        outliers.append(ModelParamOutlier(
                            symbol=symbol,
                            parameter=param_name,
                            current_value=value,
                            historical_mean=mean,
                            historical_std=std,
                            z_score=z_score,
                        ))
                        logger.warning(
                            "Model param outlier: %s.%s = %.6f (z=%.2f, mean=%.6f, std=%.6f)",
                            symbol, param_name, value, z_score, mean, std,
                        )

            # Append to history (maintain window)
            history.append(value)
            if len(history) > self.history_window:
                history.pop(0)

        return outliers

    def get_history(self, symbol: str, param: str) -> list[float]:
        """Get the historical values for a specific symbol/parameter."""
        return self._history.get(symbol, {}).get(param, [])

    def clear(self) -> None:
        """Clear all tracked history."""
        self._history.clear()
