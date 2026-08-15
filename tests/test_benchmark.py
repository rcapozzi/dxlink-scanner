"""Tests for Sprint 8: Benchmark suite."""

from __future__ import annotations

from dxlink_scanner.benchmark.perf import (
    benchmark_bayesian_update,
    benchmark_cel_evaluation,
    benchmark_end_to_end,
    benchmark_hawkes_update,
    benchmark_vectorized_bayesian,
    benchmark_vectorized_hawkes,
    percentile,
)


class TestPercentile:
    """Tests for percentile calculation."""

    def test_percentile_50(self) -> None:
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert percentile(data, 50) == 3.0

    def test_percentile_95(self) -> None:
        data = [float(x) for x in range(100)]
        result = percentile(data, 95)
        assert result == 94.05

    def test_percentile_empty(self) -> None:
        assert percentile([], 50) == 0.0

    def test_percentile_single(self) -> None:
        assert percentile([42.0], 50) == 42.0

    def test_percentile_99(self) -> None:
        data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        result = percentile(data, 99)
        assert result >= 9.0


class TestBenchmarks:
    """Tests that benchmark functions produce valid results."""

    def test_bayesian_update_benchmark(self) -> None:
        result = benchmark_bayesian_update(iterations=100)
        assert result["name"] == "bayesian_update"
        assert result["iterations"] == 100
        assert result["p50_ms"] >= 0.0
        assert result["p99_ms"] >= result["p50_ms"]
        assert result["max_ms"] >= result["p99_ms"]

    def test_hawkes_update_benchmark(self) -> None:
        result = benchmark_hawkes_update(iterations=100)
        assert result["name"] == "hawkes_update"
        assert result["iterations"] == 100
        assert result["p50_ms"] >= 0.0
        assert result["max_ms"] >= 0.0

    def test_cel_evaluation_benchmark(self) -> None:
        result = benchmark_cel_evaluation(iterations=100)
        assert result["name"] == "cel_evaluation"
        assert result["iterations"] == 100
        assert result["p50_ms"] >= 0.0

    def test_end_to_end_benchmark(self) -> None:
        result = benchmark_end_to_end(iterations=100)
        assert result["name"] == "end_to_end"
        assert result["iterations"] == 100
        assert result["p99_ms"] >= result["p50_ms"]

    def test_vectorized_bayesian_benchmark(self) -> None:
        result = benchmark_vectorized_bayesian(iterations=100)
        assert result["name"] == "vectorized_bayesian"
        assert result["iterations"] == 100
        assert result["p50_ms"] >= 0.0

    def test_vectorized_hawkes_benchmark(self) -> None:
        result = benchmark_vectorized_hawkes(iterations=100)
        assert result["name"] == "vectorized_hawkes"
        assert result["iterations"] == 100
        assert result["p50_ms"] >= 0.0
