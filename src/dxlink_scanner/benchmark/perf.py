"""Benchmark suite for measuring scanner performance.

Measures latency percentiles for:
- Model update (BayesianGammaPoisson, HawkesProcess)
- CEL rule evaluation
- End-to-end alert generation (TAS event → Alert)

Usage:
    uv run python scripts/benchmark.py --iterations 10000
    uv run python scripts/benchmark.py --output results/benchmark.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import statistics
import time
from decimal import Decimal
from pathlib import Path

from dxlink_scanner.config import CelAlertRule, DetectionConfig, WatchlistConfig
from dxlink_scanner.models import TimeAndSaleEvent
from dxlink_scanner.rules.cel_engine import CELRuleEngine
from dxlink_scanner.stats import (
    BayesianGammaPoisson,
    HawkesProcess,
)
from dxlink_scanner.stats import (
    RollingStatsManagerV2 as RollingStatsManager,
)
from dxlink_scanner.stats.vectorized import (
    VectorizedBayesianUpdater,
    VectorizedHawkesUpdater,
)

logger = logging.getLogger(__name__)


def percentile(data: list[float], p: float) -> float:
    """Compute the p-th percentile of a list of values (linear interpolation)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    if lo == hi:
        return float(sorted_data[lo])
    frac = idx - lo
    return float(sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * frac)


def benchmark_bayesian_update(iterations: int = 10000) -> dict:
    """Benchmark BayesianGammaPoisson.update() latency."""
    model = BayesianGammaPoisson(alpha=1.0, beta=1.0)
    latencies: list[float] = []

    for _ in range(iterations):
        start = time.perf_counter_ns()
        model.update(1)
        elapsed = (time.perf_counter_ns() - start) / 1e6  # Convert to ms
        latencies.append(elapsed)

    return {
        "name": "bayesian_update",
        "iterations": iterations,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": statistics.mean(latencies),
        "max_ms": max(latencies),
        "min_ms": min(latencies),
    }


def benchmark_hawkes_update(iterations: int = 10000) -> dict:
    """Benchmark HawkesProcess.add_event() latency."""
    model = HawkesProcess(mu=0.1, alpha=0.5, beta=1.0)
    t = 1000.0
    latencies: list[float] = []

    for _ in range(iterations):
        start = time.perf_counter_ns()
        model.add_event(t)
        elapsed = (time.perf_counter_ns() - start) / 1e6
        latencies.append(elapsed)
        t += 0.001  # 1ms increment

    return {
        "name": "hawkes_update",
        "iterations": iterations,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": statistics.mean(latencies),
        "max_ms": max(latencies),
        "min_ms": min(latencies),
    }


def benchmark_cel_evaluation(iterations: int = 10000) -> dict:
    """Benchmark CEL rule evaluation latency."""
    detection = DetectionConfig()
    watchlist = WatchlistConfig()
    stats_mgr = RollingStatsManager(detection)

    # Simple rule: flag trades with size > 1000

    default_rules = [
        CelAlertRule(
            name="large_print",
            expression="trade.size > 1000",
            severity="high",
        ),
        CelAlertRule(
            name="huge_print",
            expression="trade.size > 5000",
            severity="critical",
        ),
    ]

    rules = CELRuleEngine(
        detection, watchlist, stats_mgr,
        default_rules=default_rules,
    )

    event = TimeAndSaleEvent(
        symbol="SPY",
        price=Decimal("450.00"),
        size=1200,
        timestamp=dt.datetime.now(dt.UTC),
        event_type="TimeAndSale",
        delta=Decimal("0.5"),
    )

    latencies: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        rules.process(event)
        elapsed = (time.perf_counter_ns() - start) / 1e6
        latencies.append(elapsed)

    return {
        "name": "cel_evaluation",
        "iterations": iterations,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": statistics.mean(latencies),
        "max_ms": max(latencies),
        "min_ms": min(latencies),
    }


def benchmark_end_to_end(iterations: int = 10000) -> dict:
    """Benchmark end-to-end: model update + CEL evaluation + alert generation."""
    detection = DetectionConfig()
    watchlist = WatchlistConfig()
    stats_mgr = RollingStatsManager(detection)

    from dxlink_scanner.config import CelAlertRule  # noqa: F811

    default_rules = [
        CelAlertRule(
            name="bayesian_anomaly",
            expression="trade.is_option && trade.size > stats.median * 2.0",
            severity="high",
        ),
        CelAlertRule(
            name="large_print",
            expression="trade.size > 1000",
            severity="high",
        ),
    ]

    bayesian = BayesianGammaPoisson(alpha=1.0, beta=1.0)
    hawkes = HawkesProcess(mu=0.1, alpha=0.5, beta=1.0)

    rules = CELRuleEngine(
        detection, watchlist, stats_mgr,
        bayesian_models={"SPY": bayesian},
        hawkes_models={"SPY": hawkes},
        default_rules=default_rules,
    )

    t = 1000.0
    latencies: list[float] = []

    for i in range(iterations):
        start = time.perf_counter_ns()

        # Model updates
        bayesian.update(1)
        hawkes.add_event(t)
        t += 0.001

        # Rule evaluation
        event = TimeAndSaleEvent(
            symbol="SPY",
            price=Decimal("450.00"),
            size=1200 + i % 500,
            timestamp=dt.datetime.now(dt.UTC),
            event_type="TimeAndSale",
            delta=Decimal("0.5"),
        )
        rules.process(event)

        elapsed = (time.perf_counter_ns() - start) / 1e6
        latencies.append(elapsed)

    return {
        "name": "end_to_end",
        "iterations": iterations,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": statistics.mean(latencies),
        "max_ms": max(latencies),
        "min_ms": min(latencies),
    }


def benchmark_vectorized_bayesian(iterations: int = 10000) -> dict:
    """Benchmark vectorized Bayesian batch update latency."""

    symbols = [f"SYM{i:03d}" for i in range(10)]
    updater = VectorizedBayesianUpdater(symbols)
    # Pre-generate counts
    counts = {s: [1] * (iterations // len(symbols) + 1) for s in symbols}
    latencies: list[float] = []

    for _ in range(iterations):
        start = time.perf_counter_ns()
        updater.batch_update(counts)
        elapsed = (time.perf_counter_ns() - start) / 1e6
        latencies.append(elapsed)

    return {
        "name": "vectorized_bayesian",
        "iterations": iterations,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": statistics.mean(latencies),
        "max_ms": max(latencies),
        "min_ms": min(latencies),
    }


def benchmark_vectorized_hawkes(iterations: int = 10000) -> dict:
    """Benchmark vectorized Hawkes intensity computation."""
    symbols = [f"SYM{i:03d}" for i in range(10)]
    updater = VectorizedHawkesUpdater(symbols)
    t = time.perf_counter()

    latencies: list[float] = []
    for _ in range(iterations):
        for s in symbols:
            updater.add_event(s, t)
        start = time.perf_counter_ns()
        updater.compute_intensities_batch(t)
        elapsed = (time.perf_counter_ns() - start) / 1e6
        latencies.append(elapsed)
        t += 0.001

    return {
        "name": "vectorized_hawkes",
        "iterations": iterations,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": statistics.mean(latencies),
        "max_ms": max(latencies),
        "min_ms": min(latencies),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark scanner performance")
    parser.add_argument("--iterations", type=int, default=10000, help="Number of iterations per benchmark")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    results: list[dict] = []
    benchmarks = [
        ("Bayesian update", benchmark_bayesian_update),
        ("Hawkes update", benchmark_hawkes_update),
        ("Vectorized Bayesian", benchmark_vectorized_bayesian),
        ("Vectorized Hawkes", benchmark_vectorized_hawkes),
        ("CEL evaluation", benchmark_cel_evaluation),
        ("End-to-end", benchmark_end_to_end),
    ]

    for name, fn in benchmarks:
        logger.info("Running %s benchmark (%d iterations)...", name, args.iterations)
        result = fn(iterations=args.iterations)
        results.append(result)
        logger.info(
            "  %s: p50=%.4fms, p95=%.4fms, p99=%.4fms",
            result["name"], result["p50_ms"], result["p95_ms"], result["p99_ms"],
        )

    output = {
        "timestamp": dt.datetime.now(dt.UTC).isoformat(),
        "iterations": args.iterations,
        "results": results,
    }

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2))
        logger.info("Results written to %s", args.output)
    else:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
