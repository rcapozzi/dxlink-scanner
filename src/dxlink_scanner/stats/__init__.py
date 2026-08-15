"""Rolling statistics management package."""

from dxlink_scanner.models import RollingStats
from dxlink_scanner.stats.model_store import (
    CalibrationDiagnostics,
    ModelSet,
    ModelStore,
    bayesian_decision,
    hierarchical_fdr,
    online_fdr_threshold,
    prior_elicitation,
)
from dxlink_scanner.stats.rolling_v2 import RollingStatsManagerV2, RollingStatsV2
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
    "ModelStore",
    "ModelSet",
    "CalibrationDiagnostics",
    "prior_elicitation",
    "bayesian_decision",
    "online_fdr_threshold",
    "hierarchical_fdr",
]
