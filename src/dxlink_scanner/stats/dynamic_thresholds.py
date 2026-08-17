"""Dynamic threshold configuration and evaluation.

Makes production.yaml thresholds stats-driven instead of static.

Classes:
    ThresholdExpression — lightweight expression evaluator for threshold values
    DynamicThresholdManager — computes thresholds at runtime from model stats
    AdaptiveTuner — tunes thresholds based on realized FDR and alert quality
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

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


class AdaptiveTuner:
    """Tunes thresholds based on realized alert quality metrics.

    Implements a feedback loop: if FDR is too high, increase thresholds;
    if detection rate is too low, decrease thresholds. Adjustments are
    persisted to config files for learning across restarts.
    """

    def __init__(
        self,
        config: DetectionConfig,
        config_path: str | None = None,
        target_fdr: float = 0.05,
        adjustment_rate: float = 0.1,
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._target_fdr = target_fdr
        self._adjustment_rate = adjustment_rate
        self._size_mult = float(config.size_mult)
        self._vpin_threshold = float(config.vpin_threshold)
        self._fdr_alpha = float(config.fdr_alpha)

    def record_period(self, tP: int, fP: int, tN: int, fN: int) -> dict[str, float]:
        """Record results from a monitoring period and compute adjustment factors.

        Args:
            tP: True positives
            fP: False positives
            tN: True negatives
            fN: False negatives

        Returns:
            Dict with computed metrics and adjustment recommendations.
        """
        total_positives = tP + fP
        total_actual = tP + fN

        fdr = fP / total_positives if total_positives > 0 else 0.0
        tpr = tP / total_actual if total_actual > 0 else 0.0
        fpr = fP / (fP + tN) if (fP + tN) > 0 else 0.0

        size_mult_adj = 1.0
        vpin_adj = 1.0
        fdr_alpha_adj = 1.0

        if fdr > self._target_fdr:
            excess = (fdr - self._target_fdr) / self._target_fdr
            size_mult_adj = 1.0 + (self._adjustment_rate * excess)
            vpin_adj = 1.0 + (self._adjustment_rate * excess * 0.5)
            fdr_alpha_adj = max(0.5, 1.0 - (self._adjustment_rate * excess))
            logger.info(
                "FDR %.3f > target %.3f -- increasing thresholds (size_mult x%.2f)",
                fdr,
                self._target_fdr,
                size_mult_adj,
            )
        elif tpr < 0.7:
            deficit = (0.7 - tpr) / 0.7
            size_mult_adj = max(0.5, 1.0 - (self._adjustment_rate * deficit))
            vpin_adj = max(0.5, 1.0 - (self._adjustment_rate * deficit * 0.5))
            logger.info(
                "TPR %.3f < target 0.7 -- decreasing thresholds (size_mult x%.2f)",
                tpr,
                size_mult_adj,
            )

        return {
            "fdr": fdr,
            "tpr": tpr,
            "fpr": fpr,
            "size_mult_adjustment": size_mult_adj,
            "vpin_adjustment": vpin_adj,
            "fdr_alpha_adjustment": fdr_alpha_adj,
            "should_adjust": size_mult_adj != 1.0 or vpin_adj != 1.0,
        }

    def apply_adjustments(self, metrics: dict[str, float]) -> None:
        """Apply adjustment factors to the config and persist."""
        if not metrics.get("should_adjust", False):
            return

        self._size_mult *= metrics["size_mult_adjustment"]
        self._vpin_threshold = min(0.95, self._vpin_threshold * metrics["vpin_adjustment"])
        self._fdr_alpha *= metrics["fdr_alpha_adjustment"]

        self._config.size_mult = self._size_mult
        self._config.vpin_threshold = self._vpin_threshold
        self._config.fdr_alpha = self._fdr_alpha

        logger.info(
            "Adjusted: size_mult=%.3f, vpin_threshold=%.3f, fdr_alpha=%.4f",
            self._size_mult,
            self._vpin_threshold,
            self._fdr_alpha,
        )

        if self._config_path:
            self._persist()

    def _persist(self) -> None:
        """Persist adjusted thresholds back to the YAML config file."""
        import yaml

        if not self._config_path:
            return

        path = __import__("pathlib").Path(self._config_path)
        if not path.exists():
            return

        with open(path) as f:
            raw = yaml.safe_load(f)

        if raw and isinstance(raw.get("detection"), dict):
            raw["detection"]["size_mult"] = round(self._size_mult, 4)
            raw["detection"]["vpin_threshold"] = round(self._vpin_threshold, 4)
            raw["detection"]["fdr_alpha"] = round(self._fdr_alpha, 4)

            with open(path, "w") as f:
                yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

            logger.info("Persisted adaptive threshold adjustments to %s", path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "size_mult": self._size_mult,
            "vpin_threshold": self._vpin_threshold,
            "fdr_alpha": self._fdr_alpha,
            "target_fdr": self._target_fdr,
        }


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
