"""Rolling statistics management package."""

from dxlink_scanner.models import RollingStats
from dxlink_scanner.stats.rolling_v2 import RollingStatsManagerV2, RollingStatsV2

# Re-export the old RollingStatsManager for backward compatibility
from dxlink_scanner.stats.rolling_v2 import RollingStatsManagerV2 as RollingStatsManager
from dxlink_scanner.stats.seasonality import TimeOfDayAggregator
from dxlink_scanner.stats.statistical_analysis import (
    BayesianGammaPoisson,
    CrossSymbolPool,
    HawkesProcess,
    RegimeDetector,
    RegimeState,
    TimeOfDaySeasonality,
    VolumeAtPrice,
    bayesian_anomaly_score,
    false_discovery_rate_control,
)

__all__ = [
    "RollingStatsManager",
    "RollingStats",
    "RollingStatsV2",
    "RollingStatsManagerV2",
    "BayesianGammaPoisson",
    "HawkesProcess",
    "TimeOfDaySeasonality",
    "CrossSymbolPool",
    "VolumeAtPrice",
    "RegimeDetector",
    "RegimeState",
    "bayesian_anomaly_score",
    "false_discovery_rate_control",
    "TimeOfDayAggregator",
]
