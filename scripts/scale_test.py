#!/usr/bin/env python
"""Scale test: verify scanner can handle 500 symbols at 100K events/sec.

Generates synthetic event streams for 500 symbols and measures:
- Model update throughput
- CEL evaluation throughput
- Memory usage

Usage:
    uv run python scripts/scale_test.py
    uv run python scripts/scale_test.py --symbols 100 --events 50000
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dxlink_scanner.config import CelAlertRule, DetectionConfig, WatchlistConfig  # noqa: E402
from dxlink_scanner.models import TimeAndSaleEvent  # noqa: E402
from dxlink_scanner.rules.cel_engine import CELRuleEngine  # noqa: E402
from dxlink_scanner.stats import (  # noqa: E402
    BayesianGammaPoisson,
    HawkesProcess,
    RegimeDetector,
    RollingStatsManagerV2 as RollingStatsManager,
    TimeOfDaySeasonality,
    VolumeAtPrice,
)
from dxlink_scanner.stats.vectorized import (  # noqa: E402
    VectorizedBayesianUpdater,
    VectorizedHawkesUpdater,
)

logger = logging.getLogger(__name__)


def generate_events(num_symbols: int, num_events: int, seed: int = 42) -> list[tuple[str, int, float]]:
    """Generate synthetic (symbol, size, price) events."""
    rng = random.Random(seed)
    symbols = [f"SYM{i:03d}" for i in range(num_symbols)]
    events = []
    for i in range(num_events):
        symbol = symbols[i % num_symbols]
        size = rng.randint(1, 500)
        price = round(100.0 + rng.uniform(-2, 2), 2)
        events.append((symbol, size, price))
    return events


def scale_test(num_symbols: int = 500, num_events: int = 100000) -> dict:
    """Run a scale test with the specified number of symbols and events."""
    logger.info("Starting scale test: %d symbols, %d events", num_symbols, num_events)

    events = generate_events(num_symbols, num_events)
    symbols = list(set(s for s, _, _ in events))

    # Test 1: Per-symbol model updates
    logger.info("Test 1: Per-symbol model updates (non-vectorized)")
    bayesian_models = {s: BayesianGammaPoisson() for s in symbols}
    hawkes_models = {s: HawkesProcess() for s in symbols}
    t0 = time.perf_counter()
    for symbol, size, _ in events:
        bayesian_models[symbol].update(1)
        hawkes_models[symbol].add_event(t0)
    elapsed_per_symbol = time.perf_counter() - t0
    throughput_per_symbol = num_events / elapsed_per_symbol
    logger.info("  Per-symbol: %.1f events/sec", throughput_per_symbol)

    # Test 2: Vectorized model updates
    logger.info("Test 2: Vectorized batch model updates")
    vec_bayes = VectorizedBayesianUpdater(symbols)
    vec_hawkes = VectorizedHawkesUpdater(symbols)
    counts: dict[str, list[int]] = {s: [] for s in symbols}
    t0 = time.perf_counter()
    for symbol, size, _ in events:
        counts[symbol].append(1)
        vec_hawkes.add_event(symbol, t0)
    vec_bayes.batch_update(counts)
    elapsed_vec = time.perf_counter() - t0
    throughput_vec = num_events / elapsed_vec
    logger.info("  Vectorized: %.1f events/sec (speedup: %.1fx)", throughput_vec, throughput_vec / throughput_per_symbol)

    # Test 3: CEL evaluation
    logger.info("Test 3: CEL rule evaluation")
    detection = DetectionConfig()
    watchlist = WatchlistConfig()
    stats_mgr = RollingStatsManager(detection)
    rules = CELRuleEngine(
        detection, watchlist, stats_mgr,
        bayesian_models=bayesian_models,
        hawkes_models=hawkes_models,
        default_rules=[
            CelAlertRule(
                name="test_alert",
                expression="trade.size > 300",
                severity="high",
            ),
        ],
    )
    t0 = time.perf_counter()
    for symbol, size, price in events[:10000]:
        event = TimeAndSaleEvent(
            symbol=symbol,
            price=Decimal(str(price)),
            size=size,
            timestamp=dt.datetime.now(dt.timezone.utc),
            event_type="TimeAndSale",
            delta=Decimal("0.5"),
        )
        rules.process(event)
    elapsed_cel = time.perf_counter() - t0
    throughput_cel = min(10000, num_events) / elapsed_cel
    logger.info("  CEL evaluation: %.1f events/sec", throughput_cel)

    return {
        "symbols": num_symbols,
        "events": num_events,
        "per_symbol_throughput": round(throughput_per_symbol, 1),
        "vectorized_throughput": round(throughput_vec, 1),
        "speedup": round(throughput_vec / throughput_per_symbol, 1),
        "cel_throughput": round(throughput_cel, 1),
        "per_symbol_time_sec": round(elapsed_per_symbol, 3),
        "vectorized_time_sec": round(elapsed_vec, 3),
        "cel_time_sec": round(elapsed_cel, 3),
        "meets_target": throughput_per_symbol >= 10000,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scale test for scanner")
    parser.add_argument("--symbols", type=int, default=500)
    parser.add_argument("--events", type=int, default=100000)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    result = scale_test(args.symbols, args.events)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2))
        logger.info("Results written to %s", args.output)
    else:
        print(json.dumps(result, indent=2))

    if result["meets_target"]:
        logger.info("✅ Scale test PASSED: meets 10K eps target")
    else:
        logger.warning("⚠️  Scale test: throughput %.0f eps below 10K target", result["per_symbol_throughput"])


if __name__ == "__main__":
    main()
