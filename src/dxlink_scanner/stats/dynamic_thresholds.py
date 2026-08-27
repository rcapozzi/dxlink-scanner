"""Dynamic threshold configuration and evaluation.

Makes production.yaml thresholds stats-driven instead of static.

Classes:
    ThresholdExpression — lightweight expression evaluator for threshold values
    DynamicThresholdManager — computes thresholds at runtime from model stats
    AdaptiveTuner — tunes thresholds based on realized FDR and alert quality
    ConfigPersister — validates and persists config changes with locking
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from dxlink_scanner.config import DetectionConfig

if TYPE_CHECKING:
    from dxlink_scanner.stats import BayesianGammaPoisson, HawkesProcess

logger = logging.getLogger(__name__)


class ThresholdExpression:
    """Parses and evaluates simple expression-based threshold values.

    Supports expressions like:
    - "100" (static value)
    - "bayesian_mean * 5" (model-based)
    - "config_p95_size * config_size_mult" (reference other config)
    - "max(config_p95_size, 50)" (with functions)

    This is a lightweight expression evaluator — not full CEL — that allows
    thresholds to reference statistical model outputs and other config values.
    """

    _OPERATORS = {
        "+": lambda a, b: a + b,
        "-": lambda a, b: a - b,
        "*": lambda a, b: a * b,
        "/": lambda a, b: a / b if b != 0 else 0.0,
    }

    _FUNCTIONS = {
        "max": lambda *args: max(args),
        "min": lambda *args: min(args),
        "abs": lambda a: abs(a),
        "round": lambda a: round(a),
        "ceil": lambda a: math.ceil(a),
        "floor": lambda a: math.floor(a),
    }

    def __init__(self, expression: float | int | str | None) -> None:
        self._raw = expression
        self._is_static = isinstance(expression, (int, float))
        self._static_value = float(expression) if isinstance(expression, (int, float)) else None
        self._template = expression if isinstance(expression, str) else None

    def evaluate(self, context: dict[str, float]) -> float:
        """Evaluate the expression against the current stats context.

        Args:
            context: Flat dict of variable name -> value, e.g.
                {"bayesian_mean": 10.5, "p95_size": 120, "size_mult": 2.0}
        """
        if self._is_static and self._static_value is not None:
            return self._static_value
        if self._template is None:
            return 0.0

        template = self._template.strip()
        if template in context:
            return float(context[template])

        try:
            return float(template)
        except ValueError:
            pass

        try:
            return self._eval_expr(template, context)
        except Exception:
            logger.warning("Failed to evaluate threshold expression '%s', using 0.0", template)
            return 0.0

    def _eval_expr(self, expr: str, context: dict[str, float]) -> float:
        """Evaluate a simple arithmetic expression with variable references."""
        expr = expr.strip()

        for func_name, func in self._FUNCTIONS.items():
            if expr.startswith(f"{func_name}(") and expr.endswith(")"):
                args_str = expr[len(func_name) + 1 : -1]
                args = [self._eval_value(a.strip(), context) for a in args_str.split(",")]
                return float(func(*args))

        # Operators ordered by precedence: * / before + -
        for op in ["+", "-"]:
            idx = self._find_operator(expr, op)
            if idx is not None:
                left = expr[:idx].strip()
                right = expr[idx + 1 :].strip()
                left_val = self._eval_expr(left, context)
                right_val = self._eval_expr(right, context)
                return float(self._OPERATORS[op](left_val, right_val))

        for op in ["*", "/"]:
            idx = self._find_operator(expr, op)
            if idx is not None:
                left = expr[:idx].strip()
                right = expr[idx + 1 :].strip()
                left_val = self._eval_expr(left, context)
                right_val = self._eval_expr(right, context)
                return float(self._OPERATORS[op](left_val, right_val))

        return self._eval_value(expr, context)

    def _eval_value(self, token: str, context: dict[str, float]) -> float:
        """Evaluate a single value (number or variable name)."""
        token = token.strip()
        if token in context:
            return float(context[token])
        try:
            return float(token)
        except ValueError:
            return 0.0

    @staticmethod
    def _find_operator(expr: str, op: str) -> int | None:
        """Find the index of an operator at the top level (not inside parentheses).

        Skips operators adjacent to alphanumeric characters (to avoid
        matching e.g. '*' inside a variable name or number).
        """
        depth = 0
        prev_char = ""
        for i, char in enumerate(expr):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0 and expr[i : i + len(op)] == op:
                # Skip if operator is adjacent to alphanumeric (part of identifier)
                before_ok = i == 0 or not (prev_char.isalnum() or prev_char == "_")
                after_ok = (i + len(op) >= len(expr)) or not (expr[i + len(op)].isalnum() or expr[i + len(op)] == "_")
                if before_ok and after_ok:
                    return i
            prev_char = char
        return None

    @property
    def is_static(self) -> bool:
        return self._is_static

    @property
    def template(self) -> str | None:
        return self._template


class DynamicThresholdManager:
    """Computes alert thresholds at runtime from statistical model outputs.

    Replaces static thresholds in production.yaml with dynamic values
    computed from Bayesian posteriors, regime state, Hawkes intensity,
    and rolling statistics.

    Usage:
        dtm = DynamicThresholdManager(config)
        dtm.register_dynamic_thresholds({"p95_size": {"base": "bayesian_mean * 5"}})
        thresholds = dtm.compute_thresholds("SPY", bayesian_model, hawkes_model, stats)
    """

    def __init__(self, config: DetectionConfig) -> None:
        self._config = config
        self._registered: dict[str, dict[str, Any]] = {}
        self._adaptive_multipliers: dict[str, float] = {}

    def register_dynamic_thresholds(self, thresholds: dict[str, dict[str, Any]]) -> None:
        """Register expression-based threshold configurations.

        Args:
            thresholds: Dict mapping threshold name -> {
                "expression": str,  # expression referencing model outputs
                "regime_adjustment": dict[str, float],  # multiplier per regime
                "vol_target": bool,  # whether to apply volatility targeting
            }
        """
        self._registered = thresholds

    def compute_thresholds(
        self,
        symbol: str,
        bayesian: BayesianGammaPoisson | None = None,
        hawkes: HawkesProcess | None = None,
        rolling_stats: dict[str, float] | None = None,
        regime: str = "normal",
        regime_prob: float | None = None,
        vol_ratio: float | None = None,
        volatility: float | None = None,
    ) -> dict[str, float]:
        """Compute dynamic thresholds for a symbol based on current model state.

        Falls back to static config values when no dynamic threshold is
        registered for a given name.
        """
        context: dict[str, float] = {
            "abs_min_size": float(self._config.abs_min_size),
            "size_mult": float(self._config.size_mult),
            "vpin_threshold": float(self._config.vpin_threshold),
            "fdr_alpha": float(self._config.fdr_alpha),
        }

        if bayesian:
            context["bayesian_mean"] = bayesian.posterior_mean()
            context["bayesian_alpha"] = bayesian.alpha_post
            context["bayesian_beta"] = bayesian.beta_post
            context["bayesian_n"] = float(bayesian.n_observations)

        if hawkes:
            context["hawkes_intensity"] = float(hawkes._current_intensity or hawkes.mu)
            context["hawkes_mu"] = hawkes.mu

        if rolling_stats:
            context.update(rolling_stats)

        if vol_ratio is not None:
            context["vol_ratio"] = vol_ratio
        if volatility is not None:
            context["volatility"] = volatility

        thresholds: dict[str, float] = {}

        for name, spec in self._registered.items():
            if not isinstance(spec, dict):
                thresholds[name] = ThresholdExpression(spec).evaluate(context)
                continue

            expr = spec.get("expression")
            expr_eval = ThresholdExpression(expr)
            value = expr_eval.evaluate(context)

            regime_adj = float(spec.get("regime_adjustment", {}).get(regime, 1.0))
            value *= regime_adj

            if spec.get("vol_target", False) and volatility and self._config.vol_target > 0:
                vol_ratio_val = volatility / self._config.vol_target
                value *= math.sqrt(self._config.vol_target / vol_ratio_val) if vol_ratio_val > 0 else 1.0

            thresholds[name] = value

        thresholds.setdefault("size_mult", float(self._config.size_mult))
        thresholds.setdefault("abs_min_size", float(self._config.abs_min_size))

        return thresholds

    def get_threshold(self, name: str, thresholds: dict[str, float]) -> float:
        """Get a threshold value, falling back to static config."""
        if name in thresholds:
            return thresholds[name]
        return float(getattr(self._config, name, 0.0))


@dataclass(slots=True)
class RuntimeThresholds:
    """Runtime-adjusted threshold values (decoupled from Pydantic config)."""

    size_mult: float
    vpin_threshold: float
    fdr_alpha: float

    def to_dict(self) -> dict[str, float]:
        return {
            "size_mult": self.size_mult,
            "vpin_threshold": self.vpin_threshold,
            "fdr_alpha": self.fdr_alpha,
        }


class ConfigPersister:
    """Thread-safe config persistence with validation."""

    def __init__(self, config_path: str) -> None:
        self._path = Path(config_path)
        self._lock = threading.Lock()

    def persist_thresholds(
        self,
        runtime_thresholds: dict[str, RuntimeThresholds],
        detection_config: DetectionConfig,
    ) -> bool:
        """Atomically persist adjusted thresholds to YAML after validation.

        Returns True if persisted, False if no changes or error.
        """
        with self._lock:
            if not self._path.exists():
                return False

            with open(self._path) as f:
                raw = yaml.safe_load(f)

            if not raw or not isinstance(raw.get("detection"), dict):
                return False

            changed = False
            for symbol, thresholds in runtime_thresholds.items():
                key = "detection" if symbol == "default" else f"detection_{symbol}"
                if key not in raw:
                    raw[key] = {}
                if thresholds.size_mult != raw[key].get("size_mult"):
                    raw[key]["size_mult"] = round(thresholds.size_mult, 4)
                    changed = True
                if thresholds.vpin_threshold != raw[key].get("vpin_threshold"):
                    raw[key]["vpin_threshold"] = round(thresholds.vpin_threshold, 4)
                    changed = True
                if thresholds.fdr_alpha != raw[key].get("fdr_alpha"):
                    raw[key]["fdr_alpha"] = round(thresholds.fdr_alpha, 4)
                    changed = True

            if not changed:
                return False

            # Validate by re-loading through Pydantic
            from dxlink_scanner.config import ScannerConfig
            try:
                ScannerConfig.model_validate(raw)
            except Exception as e:
                logger.error("Config validation failed, not persisting: %s", e)
                return False

            # Atomic write
            tmp_path = self._path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
            tmp_path.replace(self._path)

            logger.info("Persisted adaptive threshold adjustments to %s", self._path)
            return True


class AdaptiveTuner:
    """Tunes thresholds based on realized alert quality metrics.

    Implements a feedback loop: if FDR is too high, increase thresholds;
    if detection rate is too low, decrease thresholds. Adjustments are
    persisted to config files for learning across restarts.

    Supports per-symbol tuning with regime-aware context.
    """

    def __init__(
        self,
        config: DetectionConfig,
        config_path: str | None = None,
        target_fdr: float = 0.05,
        target_tpr: float = 0.7,
        adjustment_rate: float = 0.1,
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._target_fdr = target_fdr
        self._target_tpr = target_tpr
        self._adjustment_rate = adjustment_rate

        # Per-symbol runtime thresholds (default + symbol overrides)
        self._thresholds: dict[str, RuntimeThresholds] = {
            "default": RuntimeThresholds(
                size_mult=float(config.size_mult),
                vpin_threshold=float(config.vpin_threshold),
                fdr_alpha=float(config.fdr_alpha),
            )
        }

        # Per-symbol metrics buffers
        self._metrics: dict[str, dict[str, int]] = {
            "default": {"tp": 0, "fp": 0, "alerts": 0, "events": 0}
        }

        self._persister = ConfigPersister(config_path) if config_path else None

    def _get_metrics(self, symbol: str) -> dict[str, int]:
        if symbol not in self._metrics:
            self._metrics[symbol] = {"tp": 0, "fp": 0, "alerts": 0, "events": 0}
        return self._metrics[symbol]

    def _get_thresholds(self, symbol: str) -> RuntimeThresholds:
        if symbol not in self._thresholds:
            self._thresholds[symbol] = RuntimeThresholds(
                size_mult=float(self._config.size_mult),
                vpin_threshold=float(self._config.vpin_threshold),
                fdr_alpha=float(self._config.fdr_alpha),
            )
        return self._thresholds[symbol]

    def record_event(self, symbol: str = "default") -> None:
        """Call from consumer per TAS event."""
        self._get_metrics(symbol)["events"] += 1

    def record_alert(self, is_true_positive: bool, symbol: str = "default") -> None:
        """Call from CELRuleEngine when an alert fires with known outcome."""
        m = self._get_metrics(symbol)
        m["alerts"] += 1
        if is_true_positive:
            m["tp"] += 1
        else:
            m["fp"] += 1

    def tune(self, symbol: str = "default") -> dict[str, float] | None:
        """Compute + apply adjustments for a symbol; returns metrics or None if no change."""
        m = self._get_metrics(symbol)
        tP, fP = m["tp"], m["fp"]
        alerts = m["alerts"]
        events = max(m["events"], 1)

        total_pos = tP + fP
        fdr = fP / total_pos if total_pos > 0 else 0.0
        tpr = tP / alerts if alerts > 0 else 0.0
        fpr = fP / (fP + (events - alerts)) if (fP + (events - alerts)) > 0 else 0.0

        thresholds = self._get_thresholds(symbol)

        size_mult_adj = 1.0
        vpin_adj = 1.0
        fdr_alpha_adj = 1.0
        should_adjust = False

        if fdr > self._target_fdr:
            excess = (fdr - self._target_fdr) / self._target_fdr
            size_mult_adj = 1.0 + (self._adjustment_rate * excess)
            vpin_adj = 1.0 + (self._adjustment_rate * excess * 0.5)
            fdr_alpha_adj = max(0.5, 1.0 - (self._adjustment_rate * excess))
            should_adjust = True
            logger.info(
                "FDR %.3f > target %.3f -- increasing thresholds (size_mult x%.2f)",
                fdr,
                self._target_fdr,
                size_mult_adj,
            )
        elif tpr < self._target_tpr:
            deficit = (self._target_tpr - tpr) / self._target_tpr
            size_mult_adj = max(0.5, 1.0 - (self._adjustment_rate * deficit))
            vpin_adj = max(0.5, 1.0 - (self._adjustment_rate * deficit * 0.5))
            should_adjust = True
            logger.info(
                "TPR %.3f < target %.3f -- decreasing thresholds (size_mult x%.2f)",
                tpr,
                self._target_tpr,
                size_mult_adj,
            )

        if should_adjust:
            thresholds.size_mult *= size_mult_adj
            thresholds.vpin_threshold = min(0.95, thresholds.vpin_threshold * vpin_adj)
            thresholds.fdr_alpha *= fdr_alpha_adj

            logger.info(
                "Adjusted [%s]: size_mult=%.3f, vpin_threshold=%.3f, fdr_alpha=%.4f",
                symbol,
                thresholds.size_mult,
                thresholds.vpin_threshold,
                thresholds.fdr_alpha,
            )

            # Reset metrics for this symbol
            m["tp"] = 0
            m["fp"] = 0
            m["alerts"] = 0
            m["events"] = 0

            # Persist if configured
            if self._persister:
                self._persister.persist_thresholds(self._thresholds, self._config)

        return {
            "fdr": fdr,
            "tpr": tpr,
            "fpr": fpr,
            "size_mult_adjustment": size_mult_adj,
            "vpin_adjustment": vpin_adj,
            "fdr_alpha_adjustment": fdr_alpha_adj,
            "should_adjust": should_adjust,
        }

    def get_thresholds(self, symbol: str = "default") -> RuntimeThresholds:
        """Get current runtime thresholds for a symbol."""
        return self._get_thresholds(symbol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thresholds": {k: v.to_dict() for k, v in self._thresholds.items()},
            "target_fdr": self._target_fdr,
            "target_tpr": self._target_tpr,
        }

    async def run_loop(self, interval_sec: float = 300) -> None:
        """Background task that periodically tunes all symbols."""
        while True:
            await asyncio.sleep(interval_sec)
            for symbol in list(self._metrics.keys()):
                result = self.tune(symbol)
                if result and result.get("should_adjust"):
                    logger.info(
                        "Adaptive tuning [%s]: fdr=%.3f tpr=%.3f",
                        symbol,
                        result["fdr"],
                        result["tpr"],
                    )


def build_stats_context(
    bayesian: BayesianGammaPoisson | None,
    hawkes: HawkesProcess | None,
    regime_state: Any | None,
    rolling_v2: Any | None,
) -> dict[str, float]:
    """Build a stats context dict for threshold expression evaluation.

    Aggregates all available statistical model outputs into a flat dict
    suitable for `ThresholdExpression.evaluate()`.
    """
    context: dict[str, float] = {}

    if bayesian:
        context["bayesian_mean"] = bayesian.posterior_mean()
        context["bayesian_alpha"] = bayesian.alpha_post
        context["bayesian_beta"] = bayesian.beta_post
        context["bayesian_n"] = float(bayesian.n_observations)

    if hawkes:
        context["hawkes_intensity"] = float(hawkes._current_intensity or hawkes.mu)
        context["hawkes_mu"] = hawkes.mu

    if regime_state:
        context["regime"] = float(regime_state.regime)
        context["regime_prob"] = float(regime_state.probability)
        context["volatility"] = float(regime_state.volatility)

    if rolling_v2:
        context["rolling_mean"] = float(rolling_v2.mean())
        context["rolling_std"] = float(rolling_v2.std())
        context["rolling_p95"] = float(rolling_v2.percentile(95))
        context["rolling_median"] = float(rolling_v2.median())

    return context
