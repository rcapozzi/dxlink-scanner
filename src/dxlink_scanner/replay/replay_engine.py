"""Replay framework for the DXLink scanner.

Allows replaying historical parquet event data through the statistical
models and CEL rule engine for backtesting and validation.

Usage:
    uv run python scripts/replay.py --data-dir data/events --date 2024-07-31
    uv run python scripts/replay.py --data-dir data/events --all --rules config/rules/replay.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from dxlink_scanner.config import DetectionConfig, StreamConfig, WatchlistConfig
from dxlink_scanner.models import ConsolidatedEvent, TimeAndSaleEvent, _parse_dt
from dxlink_scanner.monitoring import DataQualityMonitor
from dxlink_scanner.rules.cel_engine import CELRuleEngine
from dxlink_scanner.snapshot_store import SnapshotStore
from dxlink_scanner.stats import (
    BayesianGammaPoisson,
    CrossAssetHawkes,
    FlowMetrics,
    HawkesProcess,
    ModelSet,
    RegimeDetector,
    TimeOfDaySeasonality,
    VolumeAtPrice,
)
from dxlink_scanner.stats import (
    RollingStatsManagerV2 as RollingStatsManager,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def load_events_from_parquet(parquet_path: str | Path) -> list[ConsolidatedEvent]:
    """Load ConsolidatedEvent objects from a parquet file.

    Handles both v1 and v2 schema variants. Missing columns default to None.
    """
    path = Path(parquet_path)
    if not path.exists():
        return []

    table = pq.read_table(str(path))
    if table.num_rows == 0:
        return []

    column_names = table.column_names
    col = table.column

    events: list[ConsolidatedEvent] = []
    column_names_set = set(column_names)
    for i in range(table.num_rows):

        def get_val(col_name: str, idx: int, default=None):
            if col_name not in column_names_set:
                return default
            return col(col_name)[idx].as_py()

        event = ConsolidatedEvent(
            event_id=int(get_val("event_id", i, i) or 0),
            received_at=_parse_dt(get_val("received_at", i)) or dt.datetime.now(dt.UTC),
            source_type=get_val("source_type", i) or "TIME_AND_SALE",
            symbol=get_val("symbol", i) or "unknown",
            bid_price=Decimal(str(get_val("bid_price", i))) if get_val("bid_price", i) else None,
            ask_price=Decimal(str(get_val("ask_price", i))) if get_val("ask_price", i) else None,
            last_trade_price=Decimal(str(get_val("last_trade_price", i))) if get_val("last_trade_price", i) else None,
            last_trade_size=int(get_val("last_trade_size", i) or 0),
            last_trade_type=str(get_val("last_trade_type", i)) if get_val("last_trade_type", i) else None,
            theo_price=Decimal(str(get_val("theo_price", i))) if get_val("theo_price", i) else None,
            underlying_price=Decimal(str(get_val("underlying_price", i))) if get_val("underlying_price", i) else None,
            delta=Decimal(str(get_val("delta", i))) if get_val("delta", i) else None,
            gamma=Decimal(str(get_val("gamma", i))) if get_val("gamma", i) else None,
            event_time_ms=int(get_val("event_time_ms", i) or 0) if get_val("event_time_ms", i) is not None else None,
        )
        events.append(event)

    events.sort(key=lambda e: e.event_time_ms or 0)
    return events


def init_replay_models(
    symbols: list[str],
    vol_low: float = 0.01,
    vol_high: float = 0.03,
    vol_crash: float = 0.05,
) -> dict:
    """Initialize model sets matching the production scanner setup.

    Returns a dict with all model containers needed for replay.
    """
    all_underlyings = list(symbols) + ["default"]

    bayesian_models: dict[str, BayesianGammaPoisson] = {}
    hawkes_models: dict[str, HawkesProcess] = {}
    seasonality_models: dict[str, TimeOfDaySeasonality] = {}
    regime_detectors: dict[str, RegimeDetector] = {}
    vap_models: dict[str, VolumeAtPrice] = {}
    flow_metrics: dict[str, FlowMetrics] = {}
    model_sets: dict[str, ModelSet] = {}

    for underlying in all_underlyings:
        bayesian_models[underlying] = BayesianGammaPoisson(alpha=1.0, beta=1.0)
        hawkes_models[underlying] = HawkesProcess(mu=0.1, alpha=0.5, beta=1.0)
        seasonality_models[underlying] = TimeOfDaySeasonality()
        regime_detectors[underlying] = RegimeDetector(
            vol_low=vol_low,
            vol_high=vol_high,
            vol_crash=vol_crash,
        )
        vap_models[underlying] = VolumeAtPrice(tick_size=0.01)
        flow_metrics[underlying] = FlowMetrics(symbol=underlying)
        model_sets[underlying] = ModelSet(
            bayesian=bayesian_models[underlying],
            hawkes=hawkes_models[underlying],
            seasonality=seasonality_models[underlying],
        )

    cross_asset_hawkes = CrossAssetHawkes(symbols=all_underlyings, mu=0.1, decay=1.0)

    return {
        "bayesian_models": bayesian_models,
        "hawkes_models": hawkes_models,
        "seasonality_models": seasonality_models,
        "regime_detectors": regime_detectors,
        "vap_models": vap_models,
        "flow_metrics": flow_metrics,
        "model_sets": model_sets,
        "cross_asset_hawkes": cross_asset_hawkes,
    }


async def replay_events(
    events: list[ConsolidatedEvent],
    models: dict,
    rule_engine: CELRuleEngine,
    store: SnapshotStore,
    dq_monitor: DataQualityMonitor | None = None,
) -> dict:
    """Replay a list of events through models and rule engine.

    Returns a summary dict with alert count, event count, model updates,
    and data quality issues.
    """
    alerts: list[dict] = []
    events_processed = 0
    model_updates = 0
    gaps_detected = 0

    bayesian = models["bayesian_models"]
    hawkes = models["hawkes_models"]
    seasonality = models["seasonality_models"]
    vap_models = models["vap_models"]
    flow_metrics = models["flow_metrics"]
    cross_asset_hawkes = models["cross_asset_hawkes"]

    for event in events:
        events_processed += 1
        store.ingest(event)

        # Data quality monitoring
        if dq_monitor and event.event_time_ms:
            ts = dt.datetime.fromtimestamp(event.event_time_ms / 1000, tz=dt.UTC)
            gap = dq_monitor.record_event(event.symbol, ts)
            if gap:
                gaps_detected += 1

        # Only TAS events go to rule engine + model updates
        if event.source_type == "TIME_AND_SALE":
            snap = store.get(event.symbol)
            underlying = snap.underlying_symbol if snap and snap.underlying_symbol else event.symbol
            if underlying not in bayesian:
                underlying = "default"

            size = event.last_trade_size or 0
            trade_time = dt.datetime.now(dt.UTC).timestamp()

            # Update models
            bayesian[underlying].update(1)
            hawkes[underlying].add_event(trade_time)

            if event.last_trade_time:
                trade_dt = dt.datetime.fromtimestamp(event.last_trade_time / 1000, tz=dt.UTC)
                seasonality[underlying].add_observation(trade_dt, float(size))

            # VAP
            vap = vap_models.get(underlying)
            if vap and event.last_trade_price:
                vap.add_trade(float(event.last_trade_price), size)

            # Flow metrics
            flow = flow_metrics.get(underlying)
            if flow and snap:
                flow.update(snap, float(event.last_trade_price) if event.last_trade_price else 0.0, size)

            # Cross-asset Hawkes
            if event.last_trade_time:
                cross_asset_hawkes.add_event(underlying, event.last_trade_time / 1000.0)

            model_updates += 1

            # Rule processing
            tas_event = TimeAndSaleEvent(
                symbol=event.symbol,
                price=event.last_trade_price or Decimal("0"),
                size=size,
                timestamp=_parse_dt(event.last_trade_time) or dt.datetime.now(dt.UTC),
                event_type="TimeAndSale",
                bid_price=event.bid_price,
                ask_price=event.ask_price,
                trade_type=event.last_trade_type,
                delta=event.delta,
            )
            alert = rule_engine.process(tas_event)
            if alert:
                alerts.append(
                    {
                        "symbol": alert.symbol,
                        "rule_name": alert.rule_name,
                        "severity": alert.severity,
                        "decision_threshold": alert.decision_threshold,
                        "size": alert.size,
                    }
                )

    return {
        "events_processed": events_processed,
        "alerts": alerts,
        "alert_count": len(alerts),
        "model_updates": model_updates,
        "gaps_detected": gaps_detected,
    }


def find_date_partitions(data_dir: Path) -> list[Path]:
    """Find all date-partitioned directories."""
    if not data_dir.exists():
        return []
    partitions = []
    for d in sorted(data_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            try:
                dt.date.fromisoformat(d.name)
                partitions.append(d)
            except ValueError:
                continue
    return partitions


async def replay_date_partition(
    partition_dir: Path,
    model_factory=None,
) -> dict:
    """Replay all parquet files in a date partition.

    Args:
        partition_dir: Directory containing event parquet files for a date.
        model_factory: Optional callable returning initialized model dict.

    Returns:
        Summary dict with replay results.
    """
    parquet_files = sorted(partition_dir.glob("events_v*.parquet"))
    if not parquet_files:
        logger.warning("No parquet files found in %s", partition_dir)
        return {"error": "no files found"}

    all_events: list[ConsolidatedEvent] = []
    for pf in parquet_files:
        events = load_events_from_parquet(pf)
        all_events.extend(events)

    logger.info("Loaded %d events from %d files in %s", len(all_events), len(parquet_files), partition_dir.name)

    symbols = list(set(e.symbol for e in all_events if e.symbol))

    models = model_factory(symbols) if model_factory else init_replay_models(symbols)

    detection = DetectionConfig()
    watchlist = WatchlistConfig()
    stream_config = StreamConfig()
    store = SnapshotStore(stream_config, persist=False)
    stats_mgr = RollingStatsManager(detection)
    rules = CELRuleEngine(
        detection,
        watchlist,
        stats_mgr,
        snapshot_store=store,
        bayesian_models=models["bayesian_models"],
        hawkes_models=models["hawkes_models"],
        seasonality_models=models["seasonality_models"],
        regime_detectors=models["regime_detectors"],
        volume_at_price_models=models["vap_models"],
        flow_metrics=models["flow_metrics"],
        cross_asset_hawkes=models["cross_asset_hawkes"],
    )

    dq_monitor = DataQualityMonitor(max_gap_sec=60.0)
    result = await replay_events(all_events, models, rules, store, dq_monitor)

    result["date"] = partition_dir.name
    result["files_processed"] = len(parquet_files)
    result["symbols_seen"] = len(symbols)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay historical events through scanner models")
    parser.add_argument("--data-dir", type=Path, default=Path("data/events"))
    parser.add_argument("--date", type=str, default=None, help="Specific date (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true", help="Replay all date partitions")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON report path")
    parser.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.data_dir.exists():
        logger.error("Data directory does not exist: %s", args.data_dir)
        sys.exit(1)

    partitions = find_date_partitions(args.data_dir)
    if args.date:
        partitions = [args.data_dir / args.date]
    elif not args.all and not partitions:
        logger.error("No date partitions found")
        sys.exit(1)

    results = asyncio.run(_run_replay(partitions))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2, default=str))
        logger.info("Report written to %s", args.output)
    else:
        print(json.dumps(results, indent=2, default=str))


async def _run_replay(partitions: list[Path]) -> list[dict]:
    results = []
    for partition in partitions:
        if not partition.exists():
            logger.warning("Partition does not exist: %s", partition)
            continue
        result = await replay_date_partition(partition)
        results.append(result)
        logger.info(
            "Replay %s: %d events, %d alerts, %d gaps",
            result.get("date", "?"),
            result.get("events_processed", 0),
            result.get("alert_count", 0),
            result.get("gaps_detected", 0),
        )
    return results


if __name__ == "__main__":
    main()
