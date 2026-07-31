# SPEC: CEL-Based Dynamic Rule Engine for Options Radar Zero

**Status**: ✅ Implemented | **Author**: Hermes Agent | **Date**: 2026-08-07

---

## 1. Executive Summary

This spec describes the **CEL (Common Expression Language)** based rule evaluation system that replaced the legacy `AlertRuleEngine`. Rules are defined as CEL expressions in YAML config, enabling per-symbol and per-underlying alert logic without code changes. Per-underlying scoping (`underlying_alert_rules`) was added to apply the same rule to all option strikes of a single underlying.

**Key benefit**: Traders can define custom alert logic per underlying/option symbol (e.g., `size > 100 && price > 2.0` for SPY calls, `size > 50 && option.type == 'put'` for SPX puts) using a safe, sandboxed, microsecond-speed expression language.

---

## 2. What is CEL?

**Common Expression Language (CEL)** is a non-Turing-complete expression language designed by Google for:
- **Safety**: No loops, recursion, or side effects — guaranteed termination
- **Speed**: Nanosecond-to-microsecond evaluation (Rust backend via `cel-expr-python` or `celpy`)
- **Type safety**: Static type checking at compile time
- **Embeddability**: Designed for config-driven policy/rule evaluation

### CEL Syntax Examples

```cel
# Simple threshold
trade.size >= 100

# Compound with price filter
trade.size >= 100 && trade.price > 2.50

# Per-symbol threshold based on detection config
trade.size >= 100 && trade.price > 1.0

# Rolling stats integration
trade.size > stats.median(symbol) * 5.0

# Option-specific: type + size combo
trade.size >= 50 && option.type == "call"

# Time-aware: only alert during RTH
trade.timestamp.hour >= 9 && trade.timestamp.hour <= 16
```

### Available Python Packages (2026)

| Package | Source | Status | Notes |
|---------|--------|--------|-------|
| **cel-expr-python** | Google (official) | ✅ Active, Mar 2026 release | Native Python API, maintained by CEL team |
| **celpy** | cloud-custodian | ✅ Mature | Rust backend, widely used in cloud-custodian |
| **common-expression-language** | hardbyte | ✅ Active | Wraps Rust cel v0.14, microsecond eval |

**Recommendation**: Use `cel-expr-python` (Google's official) or `celpy` (more battle-tested).

---

## 3. Why CEL? (Pros)

| Criterion | Assessment |
|-----------|------------|
| **Safety** | ✅ Non-Turing-complete: no loops, recursion, I/O, or side effects. Guaranteed termination. |
| **Performance** | ✅ ~1-10μs/eval (Rust backend). Compiled AST cached. Beats `simpleeval`/`asteval` by 10-100x. |
| **Type System** | ✅ Static typing catches errors at compile time (not runtime). |
| **Expressiveness** | ✅ Rich stdlib: math, strings, lists, maps, timestamps, regex, IP matching. |
| **Extensibility** | ✅ Custom functions/variables via `Environment` registration. |
| **Config-Driven** | ✅ Rules as strings in YAML — no code deploy for rule changes. |
| **Auditability** | ✅ Rules are plain text — version controlled, reviewable, testable. |
| **Industry Adoption** | ✅ Used by Kubernetes (admission control), Istio, Cloud Custodian, Google Cloud. |
| **Python Integration** | ✅ `cel-expr-python` has native Python API; `celpy` wraps Rust via PyO3. |

---

## 4. Why Not CEL? (Cons / Risks)

| Risk | Mitigation |
|------|------------|
| **Learning curve** | CEL syntax differs from Python (e.g., `&&` not `and`, `||` not `or`). Provide cheatsheet + examples. |
| **No loops/recursion** | By design. For complex logic, compose multiple rules or use custom functions. |
| **Limited stdlib** | No arbitrary math (e.g., `sin`, `log`). Add via custom functions. |
| **Debugging** | Error messages can be opaque. Wrap evaluation with context logging. |
| **Dependency** | Adds Rust binary (via PyO3) or pure-Python fallback. Increases build complexity. |
| **Versioning** | CEL spec evolves. Pin package version; test on upgrade. |
| **Cold start** | First compile ~1-5ms. Cache compiled ASTs at startup. |

---

## 5. Alternatives Comparison

| Library | Safety | Speed | Expressiveness | Pythonic | Maintenance |
|---------|--------|-------|----------------|----------|-------------|
| **CEL (cel-expr-python)** | ✅ Excellent | ✅ ~1μs | ✅ Rich | ⚠️ CEL syntax | ✅ Google-backed |
| **celpy** | ✅ Excellent | ✅ ~1μs | ✅ Rich | ⚠️ CEL syntax | ✅ Cloud Custodian |
| **simpleeval** | ✅ Good | ⚠️ ~50μs | ⚠️ Basic | ✅ Python-like | ⚠️ Single maintainer |
| **asteval** | ⚠️ Moderate | ⚠️ ~30μs | ✅ Python subset | ✅ Python-like | ⚠️ lmfit (scientific) |
| **expr-lang (Go)** | ✅ Good | ✅ Fast | ✅ Good | ❌ Go only | ✅ Active |
| **Starlark** | ✅ Good | ⚠️ ~100μs | ✅ Python-like | ✅ Very Pythonic | ✅ Bazel/Google |
| **Python `eval()` + AST** | ❌ Unsafe | ✅ Fast | ✅ Full Python | ✅ Native | ❌ Don't do this |

**Verdict**: **CEL is the best fit** for production rule evaluation — unmatched safety/performance balance, industry-proven, config-native.

**Runner-up**: **Starlark** (`starlark-py`) if you want Python-like syntax and can tolerate ~100μs/eval. Used by Bazel, Buck, Sorbet.

---

## 6. Proposed Architecture

### 6.1 Config Schema (YAML)

```yaml
watchlist:
  default_alert_rules: []  # Global fallback CEL rules (optional)
  tickers:
    - symbol: "SPY"
      option_type: "equity"
      strikes_around_atm: 10
      # Per-symbol CEL rules
      alert_rules:
        - name: "large_option_print"
          expression: |
            trade.size >= 100 && trade.price > 1.0
          severity: "high"
        - name: "unusual_call_activity"
          expression: |
            trade.size >= 50 && option.type == "call"
          severity: "medium"

    - symbol: "SPX"
      option_type: "equity"
      strikes_around_atm: 20
      alert_rules:
        - name: "large_index_print"
          expression: |
            trade.size >= 50 && trade.price > 5.0
          severity: "high"

# Global default rules (apply if no per-symbol rule matches)
# Note: default_alert_rules is a watchlist-level field, not a top-level field
default_alert_rules:
  - name: "absolute_size"
    expression: "trade.size >= 1000"
    severity: "critical"
  - name: "anomaly_detector"
    expression: "trade.size > stats.median(symbol) * 5.0"
    severity: "high"
```

### 6.2 Variable Binding Context

At evaluation time, each rule receives an **activation** (variable binding):

```python
activation = {
    # Core trade fields
    "trade": {
        "symbol": "SPY250731C00450000",
        "price": Decimal("2.50"),
        "size": 150,
        "timestamp": datetime(2025, 7, 31, 14, 30, tzinfo=UTC),
        "bid_price": Decimal("2.45"),
        "ask_price": Decimal("2.55"),
        "trade_type": "buy",
    },
    # Option metadata (if available)
    "option": {
        "type": "call",           # "call" | "put"
        "strike": 450.0,
        "expiration": "2025-07-31",
    },
    # Underlying info
    "underlying": {
        "symbol": "SPY",
        "price": 451.25,
    },
    # Rolling stats (pre-computed)
    "stats": {
        "median": lambda sym: rolling_median.get(sym, 0),
        "mad": lambda sym: rolling_mad.get(sym, 0),
        "count": lambda sym: rolling_count.get(sym, 0),
        "mean": lambda sym: rolling_mean.get(sym, 0),
    },
    # Config thresholds (detection config)
    "config": {
        "abs_min_size": 10,
        "size_mult": 5.0,
    },
    # Helper functions
    "is_rth": lambda ts: 9 <= ts.hour <= 16,
}
```

### 6.3 Rule Evaluation Flow

```
┌─────────────────────┐
│ TimeAndSaleEvent    │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Resolve symbol →    │
│ underlying ticker   │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Get applicable rules│
│ (per-symbol →       │
│  underlying →       │
│  defaults)          │
└──────────┬──────────┘
           ▼
┌─────────────────────┐     ┌──────────────────┐
│ For each rule:      │────▶│ Compile CEL AST  │
│ 1. Build activation │     │ (cached at init) │
│ 2. Evaluate         │     └────────┬─────────┘
│ 3. If true → Alert  │              ▼
└─────────────────────┘     ┌──────────────────┐
                            │ Evaluate AST     │
                            │ (microseconds)   │
                            └────────┬─────────┘
                                     ▼
                            ┌──────────────────┐
                            │ Result: bool     │
                            └────────┬─────────┘
                                     ▼
                    ┌────────────────┴────────────────┐
                    ▼                                 ▼
            ┌───────────────┐                 ┌───────────────┐
            │ TRUE: Emit    │                 │ FALSE: Next   │
            │ Alert(severity)                 │ rule / None   │
            └───────────────┘                 └───────────────┘
```

### 6.4 Caching Strategy

- **Compile once at startup**: Parse all rule expressions → AST → `Program` objects
- **Cache keyed by rule name**: `Dict[str, Program]`
- **Recompile on config reload**: Hot-reload support via file watcher
- **Expected**: 50-100 rules → <5ms total compile time at startup

---

## 7. Migration Path

### Phase 1: Sidecar Engine ✅ IMPLEMENTED
- `AlertRuleEngine` removed; `CELRuleEngine` is the sole engine
- No feature flag needed — CEL is the only engine

### Phase 2: Config Migration
- Add `alert_rules` to `TickerConfig` (optional)
- Add `underlying_alert_rules` to `TickerConfig` for per-underlying rules
- Add `default_alert_rules` to `WatchlistConfig` for global fallback

### Phase 3: Full Cutover ✅ COMPLETED
- `AlertRuleEngine` removed — `CELRuleEngine` is the sole engine
- `abs_min_size`, `size_mult` retained in `DetectionConfig` and exposed as `config.*` variables in CEL expressions
- All rule evaluation through CEL

---

## 8. Implementation Tasks

| Task | Effort | Notes |
|------|--------|-------|
| Add `cel-expr-python` or `celpy` dependency | 1h | PyPI, Rust backend via PyO3 |
| Create `CELRuleEngine` class | 4h | Mirrors `AlertRuleEngine` interface |
| Define activation variable schema | 2h | Use `celpy.celtypes` or `cel.expr` types |
| Implement rule compilation cache | 2h | `lru_cache` or dict keyed by expr string |
| Add per-symbol `alert_rules` to config schema | 1h | Pydantic model with `expression: str` |
| Auto-generate CEL from legacy config | 2h | Migration helper |
| Unit tests for CEL evaluation | 4h | Edge cases: missing vars, type errors, timeouts |
| Integration test with live data | 2h | Compare alerts vs legacy engine |
| Documentation + examples | 2h | Cheatsheet, common patterns |

**Total**: ~20h / 2-3 days

---

## 9. Security Considerations

| Threat | CEL Mitigation | Additional Mitigation |
|--------|----------------|----------------------|
| Infinite loops | ✅ Impossible (no loops) | N/A |
| Memory exhaustion | ✅ Bounded by expression size | Limit max expression length (e.g., 2KB) |
| CPU DoS | ✅ Guaranteed termination | Timeout wrapper (e.g., 1ms max) |
| Data exfiltration | ✅ No I/O, no network | No filesystem/net access in stdlib |
| Code injection | ✅ Not Turing-complete | Validate AST before execution |
| Variable injection | ✅ Explicit activation | Never pass `globals()` or `locals()` |

---

## 10. Open Questions

1. **Package choice**: `cel-expr-python` (Google official, newer) vs `celpy` (battle-tested, cloud-custodian)?
2. **Custom functions**: How to expose `stats.median(symbol)` — as CEL function or pre-computed variable?
3. **Hot reload**: File watcher on config.yaml? Or SIGHUP?
4. **Rule versioning**: Store rule hash in alert for audit trail?
5. **Metrics**: Track eval latency per rule (Prometheus histogram)?
6. **Fallback**: N/A — `CELRuleEngine` supports chained tiers (per-symbol → underlying → default)

---

## 11. Recommendation

**Proceed with CEL** using `cel-expr-python` (Google's official package, native Python API, actively maintained). It provides the best balance of:
- **Safety** (non-Turing-complete, no side effects)
- **Performance** (microsecond eval, Rust backend)
- **Expressiveness** (rich stdlib, custom functions)
- **Operability** (config-driven, auditable, version-controlled)

The migration can be done incrementally behind a feature flag with zero risk to production.

---

## Appendix: CEL Syntax Cheatsheet

```cel
# Literals
42, 3.14, "hello", true, false, null

# Operators
+, -, *, /, %           # arithmetic
==, !=, <, <=, >, >=    # comparison
&&, ||, !               # logical (NOT and/or/or)
in                      # membership: x in list/map

# Ternary
condition ? true_val : false_val

# Member access
obj.field, obj["key"], list[index]

# Functions (stdlib)
size(list), has(obj.field), timestamp("2025-01-01T00:00:00Z")
duration("300s").seconds()
math.ceil(3.14), math.max(a, b)

# Custom (registered in Environment)
is_rth(timestamp)

# Option types
trade.size >= 100 && option?.type == "call"
```

---

*End of Spec*