"""Rolling statistics management package."""

from dxlink_scanner.models import RollingStats
from dxlink_scanner.stats.rolling_v2 import RollingStatsManagerV2, RollingStatsV2

# Re-export the old RollingStatsManager for backward compatibility
from dxlink_scanner.stats.rolling_v2 import RollingStatsManagerV2 as RollingStatsManager

__all__ = [
    "RollingStatsManager",
    "RollingStats",
    "RollingStatsV2",
    "RollingStatsManagerV2",
]
