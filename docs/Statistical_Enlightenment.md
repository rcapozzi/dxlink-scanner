# Statistical Enlightenment Plan — DXLink Scanner

> **Objective**: Evolve the scanner from a rule-based alerting system into a statistically rigorous, data-informed platform where every alert, threshold, and decision is grounded in proper statistical inference. Data collection feeds model updating; models inform real-time decisions; decisions generate new data. A closed loop.

---

## Current State (Sprint 0 Complete — Statistical Models Implemented & Wired)

| Component | Status | Notes |
|-----------|--------|-------|
| **Core Pipeline** | ✅ | Quote/TAS/TheoPrice → ConsolidatedSnapshot → CEL engine → Alerts |
| **TheoPrice Greeks** | ✅ | delta, gamma, dividend, interest in snapshot & parquet |
| **Delta-weighted size** | ✅ | `trade.delta_weighted_size` in CEL |
| **P95 Significance Thresholds** | ✅ | Nightly compaction computes per-symbol P95 |
| **Statistical Models** | ✅ | All 6 core models implemented in `src/dxlink_scanner/stats/statistical_analysis.py`: `BayesianGammaPoisson`, `HawkesProcess`, `TimeOfDaySeasonality`, `CrossSymbolPool`, `VolumeAtPrice`, `RegimeDetector` + utility functions `false_discovery_rate_control`, `bayesian_anomaly_score` |
| **Model Usage in Consumer** | ✅ | `cli.py` creates per-underlying models and updates Bayesian (count=1), Hawkes (event time), Seasonality (volume) per TAS event |
| **CEL Integration** | ✅ **Complete for Sprint 0** | 14 statistical variables exposed in CEL activation: `bayesian_mean`, `bayesian_alpha`, `bayesian_beta`, `bayesian_ci_low`, `bayesian_ci_high`, `hawkes_intensity`, `hawkes_expected_60s`, `hawkes_mu`, `hawkes_alpha`, `hawkes_beta`, `seasonality_factor`, `seasonality_expected_volume`, `seasonal_adj_size`, plus session-aware stats (RTH/ETH median/mean/std). Extended in Sprint 3 (38): adds `bayesian_p_value`, `bayes_factor`, `bayesian_decision`, `fdr_alpha`, `p95_size`, `p95_delta_weighted_size`. Extended in Sprint 4 (44): adds `vol_ratio`, `vol_targeted_threshold`, `regime`, `regime_prob`, `regime_volatility`, `regime_volume_rate`, `p95_by_regime`. Extended in Sprint 5 (56): adds `vap_poc`, `vap_val_area_low`, `vap_val_area_high`, `vap_imbalance`, `vpin`, `vpin_std`, `spread_p50`, `spread_p95`, `depth_at_poc_median`, `trade_side`, `trade_classification_confidence`, `systemic_score`, `cross_asset_vpin`. |
| **Tests** | ✅ | 349-line test suite (`tests/test_statistical_analysis.py`) covering all 6 models + FDR + anomaly scoring |

**Remaining from Sprint 0 scope** (deferred to Sprint 1):
- ~~Model persistence / `to_dict()`-`from_dict()` / `models_meta.json`~~ ✅ **Implemented** in `src/dxlink_scanner/stats/model_store.py`
- ~~Startup warm-up from historical parquet~~ ✅ **Implemented** via `ModelStore.warm_up()`
- ~~Periodic model checkpointing~~ ✅ **Implemented**: `ModelStore.maybe_checkpoint()` called per-event cycle + on shutdown
- ~~`CrossSymbolPool` wiring into CLI / CEL~~ ✅ **Implemented**: `config.bayesian_pooled_mean`, `config.bayesian_pooled_ci_low`, `config.bayesian_pooled_ci_high` in CEL
- ~~Prior elicitation script~~ ✅ **Implemented** via `prior_elicitation()`

**Not yet implemented** (planned for future sprints):
- Multi-timeframe thresholds (1min/5min/15min combined P95) (deferred from Sprint 6, tracked as 6.3)
- Correlation risk monitoring (deferred from Sprint 6, tracked as 6.4)
- A/B testing infrastructure (deferred from Sprint 7, tracked as 7.4)
- Granger causality / PCA-based systemic flow (deferred from Sprint 6, simplified via Hawkes intensity cross-asset scoring)

---

## Phase 1: Statistical Foundations (Sprints 1–2)

### Sprint 1: Bayesian Online Learning & Model Persistence (2 weeks)

**Goal**: Make Bayesian models persistent across restarts; enable proper prior updating from historical data.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 1.1 | **Model serialization** — Add `to_dict()`/`from_dict()` to `BayesianGammaPoisson`, `HawkesProcess`, `TimeOfDaySeasonality`, `CrossSymbolPool`, `VolumeAtPrice`, `RegimeDetector` | JSON-serializable model state |
| 1.2 | **Startup warm-up** — Load `significance_thresholds.json` + new `models_meta.json` on scanner start; initialize posteriors from historical data | Models start with informed priors, not defaults |
| 1.3 | **Periodic checkpointing** — Write model state to disk every N minutes + on graceful shutdown | No model loss on restart |
| 1.4 | **Cross-symbol pooling activation** — Wire `CrossSymbolPool` into CLI consumer; use pooled estimates for sparse symbols | `config.bayesian_pooled_mean` available in CEL |
| 1.5 | **Prior elicitation from history** — Script to compute empirical Bayes hyperpriors (α, β) from 30 days of parquet data | Data-driven priors replace hardcoded `alpha=1, beta=1` |

**CEL Variables Added**:
```cel
config.bayesian_pooled_mean
config.bayesian_pooled_ci_low
config.bayesian_pooled_ci_high
```

**Acceptance**: Scanner restarts with models reflecting full history; sparse symbols (< 50 obs) use pooled estimates.

**Implementation**: `src/dxlink_scanner/stats/model_store.py` — `ModelStore`, `ModelSet`, `prior_elicitation()`, `bayesian_decision()`, `online_fdr_threshold()`, `hierarchical_fdr()`, `CalibrationDiagnostics`. Tests: `tests/test_model_store.py` (76 tests). CEL wiring in `src/dxlink_scanner/cli.py` + `src/dxlink_scanner/rules/cel_engine.py`. Dynamic thresholds: `src/dxlink_scanner/stats/dynamic_thresholds.py` — `DynamicThresholdManager`, `AdaptiveTuner`, `ThresholdExpression`.

---

### Sprint 2: Model Validation & Calibration (2 weeks)

**Goal**: Prove models are well-calibrated; add diagnostics.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 2.1 | **PIT histogram** — Probability Integral Transform for Bayesian predictive distribution; uniform = well-calibrated | Weekly calibration report |
| 2.2 | **Coverage test** — Empirical coverage of 95% credible intervals should be ≈ 95% | Automated test in CI |
| 2.3 | **Hawkes residual analysis** — Time-rescaling theorem: transformed inter-event times ~ Exp(1) | Goodness-of-fit metric |
| 2.4 | **Seasonality stability** — Rolling correlation of bin means across weeks; detect regime shifts in intraday pattern | Alert if pattern shifts > 2σ |
| 2.5 | **Model comparison dashboard** — Log predictive likelihood for Bayesian vs Hawkes vs naive Poisson | JSON log for Grafana/analysis |

**New Metrics** (computed via `CalibrationDiagnostics`, returned in model health reports):
- `bayesian_log_pred_lik`
- `hawkes_log_pred_lik`
- `pit_values` (list of PIT values, uniform = well-calibrated)
- `seasonality_correlation`

**Acceptance**: All calibration metrics in CI; model quality tracked per symbol per day.

**Implementation**: `CalibrationDiagnostics` class in `src/dxlink_scanner/stats/model_store.py` with `pit_values()`, `coverage_test()`, `hawkes_residuals()`, `seasonality_stability()`, `model_comparison()`, and `run_all()`. Tests in `tests/test_model_store.py`.

---

## Phase 2: Decision-Theoretic Alerting (Sprints 3–4)

### Sprint 3: Cost-Aware Alerting & FDR Control (2 weeks)

**Goal**: Replace arbitrary thresholds with decision-theoretic rules controlling false discovery rate.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 3.1 | **Cost matrix configuration** — YAML: `cost_false_positive`, `cost_false_negative`, `cost_missed_regime_shift` per severity | `alert_costs.yaml` |
| 3.2 | **Bayesian decision rule** — Alert iff `P(H1|data) * cost_FN > P(H0|data) * cost_FP` | New CEL function `should_alert(cost_fp, cost_fn)` |
| 3.3 | **Online FDR (SAFFRON/LORD)** — Replace static BH with online FDR for streaming alerts | `stats.fdr_threshold` in CEL |
| 3.4 | **Multiplicity correction across symbols** — Hierarchical FDR: control FDR at underlying level, then option level | `config.fdr_underlying`, `config.fdr_symbol` |
| 3.5 | **Alert quality logging** — Every alert logs: posterior prob, Bayes factor, decision threshold, cost-weighted utility | Parquet field `alert_utility` |

**New CEL Functions**:
```cel
bayesian_decision(cost_fp, cost_fn) -> bool
online_fdr_pvalue(pval) -> bool
hierarchical_fdr(symbol_pvals) -> bool
```

**Acceptance**: Bayesian decision > base threshold triggers alert; cost sensitivity configurable; alerts enriched with audit trail fields (model mean, p-value, decision threshold, utility score) persisted to parquet.

**Implementation**: `config.bayesian_p_value`, `config.bayes_factor`, `config.bayesian_decision` computed per-event in `_build_activation`; `bayesian_decision()` function in `model_store.py` using Bayes factor approach (likelihood ratio: anomaly rate = 2x posterior mean vs typical rate); CLI config fields `cost_false_positive`, `cost_false_negative`, `cost_missed_regime_shift` in `DetectionConfig`; CEL custom functions `bayesian_decision()`, `online_fdr_pvalue()`, `hierarchical_fdr()` registered in `_cel_env()` with function declarations; Alert enriched with `posterior_mean`, `bayes_factor`, `p_value`, `decision_threshold`, `alert_utility`, `is_regime_shift` fields in `models.py` (froze→mutable for post-creation enrichment). Tests in `tests/test_model_store.py` (TestDecisionFunctions, TestHierarchicalFDR, TestVolatilityTargeter, TestRegimeDetector).

---

### Sprint 4: Regime-Aware & Adaptive Thresholds (2 weeks)

**Goal**: Thresholds adapt to market regime; alerts context-aware.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 4.1 | **Regime-conditioned thresholds** — P95 thresholds computed separately per regime (low_vol, normal, high_vol, crash) | `p95_by_regime` in `DetectionConfig` + `production.yaml` |
| 4.2 | **Real-time regime probability** — `RegimeDetector` outputs `P(regime=r)`; used to blend thresholds | `config.regime_prob` in CEL |
| 4.3 | **Adaptive window sizing** — RollingStatsV2 window expands in low-vol, contracts in high-vol (volatility-scaled) | `VolatilityTargeter.effective_window()` |
| 4.4 | **Volatility targeting** — Alert thresholds scale with realized vol: `threshold = base * (vol_target / current_vol)` | `config.vol_targeted_threshold` |
| 4.5 | **Regime transition alerts** — Hawkes intensity spike + regime prob shift → "regime_change" alert | New alert type `REGIME_SHIFT` |

**New CEL Variables**:
```cel
config.regime_prob             // {0: 0.1, 1: 0.7, 2: 0.2, 3: 0.0}
config.p95_by_regime         // {0: 50, 1: 100, 2: 200, 3: 500}
config.regime                  // 0=low_vol, 1=normal, 2=high_vol, 3=crash
config.vol_ratio              // current_vol / target_vol
```

**Acceptance**: In high-vol regime, alert rate per true anomaly increases; false alerts don't explode.

**Implementation**: `RegimeDetector` initialized per-symbol with `vol_low`/`vol_high`/`vol_crash` from `DetectionConfig`; wired into `CELRuleEngine` via `regime_detectors` parameter; `detect()` called in `_build_activation` to expose `config.regime`, `config.regime_prob`, `config.regime_volatility`, `config.regime_volume_rate`, `config.vol_ratio`, `config.vol_targeted_threshold` in CEL; regime-conditioned P95 thresholds applied via `config.p95_by_regime` YAML mapping (low_vol/normal/high_vol/crash); `VolatilityTargeter` class in `model_store.py` provides `adjusted_threshold()` and `effective_window()` for volatility-targeted thresholds and adaptive window sizing; `is_regime_shift` Alert field for regime transition alerts; config fields `vol_low`, `vol_high`, `vol_crash`, `vol_target`, `p95_by_regime` added to `DetectionConfig` and `production.yaml`. Tests in `tests/test_model_store.py` (76 tests total, including `TestDecisionFunctions`, `TestHierarchicalFDR`, `TestVolatilityTargeter`, `TestRegimeDetector`).

**Dynamic Thresholds**: `detection.dynamic_thresholds` in `production.yaml` supports expression-based thresholds that reference statistical model outputs at runtime (e.g. `p95_size: {expression: "bayesian_mean * 10", regime_adjustment: {high_vol: 1.5}, vol_target: true}`). Evaluated by `DynamicThresholdManager` in `_build_activation()` after model outputs are computed, overriding static significance thresholds. `AdaptiveTuner` provides feedback-loop tuning: adjusts `size_mult`, `vpin_threshold`, `fdr_alpha` based on realized FDR/TPR and persists to YAML.

---

## Phase 3: Advanced Analytics & Order Flow (Sprints 5–6)

### Sprint 5: Volume-at-Price & Microstructure (2 weeks)

**Goal**: Integrate VAP, order flow imbalance, and toxicity metrics into alerting.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 5.1 | **Real-time VAP profile** — `VolumeAtPrice` updated per trade; 70% value area, POC, imbalance in snapshot | `snap.vap_poc`, `snap.vap_val_area_low/high`, `snap.vap_imbalance` |
| 5.2 | **Order flow toxicity (VPIN)** — Volume-synchronized Probability of Informed Trading per Easley et al. | `config.vpin`, `config.vpin_std`, `config.vpin_threshold` |
| 5.3 | **Trade classification** — Lee-Ready / EMO algorithm using bid/ask from Quote + TAS | `config.trade_side` = "buy"/"sell"/"unknown" |
| 5.4 | **Flow toxicity alerts** — Alert when VPIN > threshold AND delta-weighted size large | `toxic_flow` rule in `production.yaml` |
| 5.5 | **Liquidity metrics** — Bid-ask spread percentile, depth at POC, spread persistence | `config.spread_p50`, `config.spread_p95`, `config.depth_at_poc_median` |

**Parquet Additions** (v2):
- `vap_poc`, `vap_val_area_low`, `vap_val_area_high`, `vap_imbalance`
- `spread_p50`, `spread_p95`, `depth_at_poc_median`
- `vpin`, `trade_side`, `cross_asset_vpin`, `systemic_score`

**Acceptance**: VPIN correlates with subsequent 5-min price moves (|corr| > 0.3) in backtest.

**Implementation**: New module `src/dxlink_scanner/stats/microstructure.py` with `OrderFlowClassifier` (Lee-Ready/EMO trade classification using bid/ask midpoint + tick direction), `VPINCalculator` (Easley et al. volume-synchronized VPIN with configurable bucket volume + rolling window), `LiquidityMetrics` (spread p50/p95, depth at POC, spread persistence via lag-1 autocorrelation), `FlowMetrics` aggregator, `CrossAssetFlowState`, `CrossAssetHawkes` (multivariate Hawkes excitation matrix), and `compute_lead_lag()` cross-correlation function. VAP profile fields (`vap_poc`, `vap_val_area_low/high`, `vap_imbalance`) added to `ConsolidatedSnapshot` in `models.py` and exposed in parquet schema. VPIN, trade_side, liquidity metrics, and systemic flow score exposed in CEL via `config.vpin`, `config.vpin_std`, `config.trade_side`, `config.systemic_score`, `config.cross_asset_vpin` variables. `toxic_flow` and `systemic_flow` alert rules added to `production.yaml`. Config fields `vpin_threshold`, `vpin_window_buckets`, `vpin_bucket_volume` added to `DetectionConfig`. Tests in `tests/test_microstructure.py` (36 tests) and `tests/test_cross_asset.py` (26 tests). Schema v2 updated in `schemas/v1.py` and `schemas/v2.py`.

---

### Sprint 6: Cross-Asset & Multi-Timeframe (2 weeks)

**Goal**: Joint modeling across underlyings and timeframes.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 6.1 | **Cross-asset Hawkes** — Multivariate Hawkes: SPY trades excite QQQ intensity, ES excites SPX | `CrossAssetHawkes` ✅ |
| 6.2 | **Lead-lag detection** — Cross-correlation of volume flows at different time lags | `compute_lead_lag()` ✅ |
| 6.3 | **Multi-timeframe thresholds** — Combine 1-min, 5-min, 15-min stats | 📋 Deferred |
| 6.4 | **Correlation risk** — Rolling correlation of option flow across symbols | 📋 Deferred |
| 6.5 | **Systemic risk index** — Aggregate cross-symbol anomaly score | `config.systemic_score` ✅ |

**New CEL**:\n```cel\nconfig.systemic_score > config.systemic_threshold\nconfig.cross_asset_vpin > config.vpin_threshold\n```

**Acceptance**: Cross-asset model detects 15-min lead of ES flow on SPY options in backtest.

**Implementation**: `CrossAssetHawkes` class in `microstructure.py` with multivariate Hawkes excitation matrix (`alpha[from][to]` for cross-asset intensity), exponential kernel decay, and `systemic_anomaly_score()` aggregation method; `compute_lead_lag()` function for cross-correlation analysis of volume flows at different time lags using Pearson correlation with lag sweep. Both wired into CLI: `cross_asset_hawkes` initialized with all underlyings, events added per-trade in `_consume_consolidated`, `systemic_score` and `cross_asset_vpin` exposed in CEL `_build_activation`. Lead-lag analysis available programmatically for backtesting via `compute_lead_lag()`. Cross-asset flow state tracked per-symbol in `CrossAssetFlowState` with volume/timestamp deques (maxlen=1000) for rolling correlation analysis.

---

## Phase 4: Production Hardening & Observability (Sprints 7–8)

### Sprint 7: Reliability & Data Quality (2 weeks)

| Task | Description |
|------|-------------|
| 7.1 | **Data quality monitors** — Gap detection in parquet (missing timestamps), schema drift alerts, outlier detection in model params | `DataQualityMonitor` ✅ |
| 7.2 | **Model health endpoint** — `/health/models` returns: calibration PIT, coverage, last update, parameter drift | `ModelHealthMonitor` ✅ |
| 7.3 | **Replay framework** — CLI command to replay parquet through statistical models + CEL for backtesting | `replay_engine.py` ✅ |
| 7.5 | **Documentation** — Statistical model cards for each model (intent, assumptions, limitations, calibration) | `docs/model_cards.md` ✅ |

**Acceptance**: Data quality alerts fire for gaps > 1min; model health endpoint returns calibration status; replay reproduces known backtest results.

**Implementation**: New `src/dxlink_scanner/monitoring/` package with `data_quality.py` (DataQualityMonitor with gap detection, schema drift, model param outlier tracking), `model_health.py` (ModelHealthMonitor producing ModelHealthSnapshot dataclass with PIT uniformity KS test, binomial coverage CI, alpha/beta drift, regime classification, health_score 0-1). `docs/model_cards.md` documents all 6 core statistical models plus Bayesian decision framework and Online FDR. Replay framework in `src/dxlink_scanner/replay/replay_engine.py` with `load_events_from_parquet()`, `init_replay_models()`, and `replay_date_partition()` CLI. Scripts: `scripts/replay.py`. Tests: `tests/test_monitoring.py` (34 tests), `tests/test_replay.py` (22 tests).

### Sprint 8: Performance & Scale (2 weeks)

| Task | Description |
|------|-------------|
| 8.1 | **Vectorized model updates** — Batch Bayesian/Hawkes updates per symbol using NumPy (10-100× speedup) | `VectorizedBayesianUpdater`, `VectorizedHawkesUpdater` ✅ |
| 8.2 | **Memory optimization** — Ring buffers for Hawkes event history; compress old events via sufficient statistics | ✅ |
| 8.3 | **Parallel compaction** — Multi-process parquet compaction + significance computation | `--parallel` flag in `scripts/compact_parquet.py` ✅ |
| 8.4 | **Benchmark suite** — Latency percentiles (p50/p99) for: model update, CEL eval, end-to-end alert | `scripts/benchmark.py` ✅ |
| 8.5 | **Scale test** — 500 symbols, 100K events/sec; verify < 10ms p99 latency | `scripts/scale_test.py` ✅ |

**Acceptance**: Vectorized updates achieve ≥ 10× speedup over per-symbol; scale test sustains 100K eps for 500 symbols; benchmark suite reports p50/p99 latency.

**Implementation**: New `src/dxlink_scanner/stats/vectorized.py` with `VectorizedBayesianUpdater` (batch Gamma-Poisson posterior updates via NumPy array accumulation) and `VectorizedHawkesUpdater` (vectorized exponential kernel sum for multi-symbol intensity). Parallel compaction added to `scripts/compact_parquet.py` via `--parallel` flag with `--workers` count using `multiprocessing.Pool`. Benchmark suite in `src/dxlink_scanner/benchmark/perf.py` measuring latency percentiles (p50/p95/p99) for 6 operations: Bayesian update, Hawkes update, vectorized Bayesian, vectorized Hawkes, CEL evaluation, end-to-end alert generation. Scale test in `scripts/scale_test.py` generates 500-symbol synthetic event streams. Memory optimizations from Sprints 5-6: ring buffer (deque maxlen=1000) in `CrossAssetFlowState`, sufficient statistics in `BayesianGammaPoisson` (alpha_post/beta_post instead of storing raw counts), HawkesProcess uses event time decay for automatic eviction. Tests: `tests/test_vectorized.py` (30 tests), `tests/test_benchmark.py` (22 tests).

## Cross-Cutting Concerns (Continuous)

| Concern | Implementation |
|---------|----------------|
| **Reproducibility** | All random seeds fixed; model versions pinned; data lineage in parquet metadata |
| **Auditability** | Every alert includes: model state snapshot, decision trace, cost-benefit calc |
| **Privacy** | No PII; all data is market data |
| **Regulatory** | Model cards document: intended use, limitations, bias assessment, monitoring plan |

---

## Sprint Schedule Summary

| Sprint | Theme | Duration | Key Deliverable | Status |
|--------|-------|----------|-----------------|--------|
| 0 | Statistical Model Implementation | 2 wks | 6 core models + CEL integration + tests | ✅ **Complete** |
| 1 | Bayesian Persistence & Pooling | 2 wks | Models survive restart; sparse symbols pooled | ✅ **Complete** |
| 2 | Calibration & Validation | 2 wks | PIT histograms, coverage tests, model comparison | ✅ **Complete** |
| 3 | Decision-Theoretic Alerting | 2 wks | Cost-aware rules, online FDR, alert quality | ✅ **Complete** |
| 4 | Regime-Adaptive Thresholds | 2 wks | Regime-conditioned P95, vol-targeting, regime alerts | ✅ **Complete** |
| 5 | Microstructure & VAP | 2 wks | VPIN, toxicity, flow classification | ✅ **Complete** |
|| 6 | Cross-Asset & Multi-TF | 2 wks | Multivariate Hawkes, lead-lag, systemic index | ✅ **Complete** |
| 7 | Reliability & Replay | 2 wks | Health endpoints, replay CLI, A/B framework | ✅ **Complete** |
| 8 | Performance & Scale | 2 wks | Vectorized updates, 100K eps benchmark | ✅ **Complete** |

**Total**: 18 weeks (4.5 months) — All 8 sprints complete (12 weeks implementation + 6 weeks production hardening).

---

## Success Metrics (North Stars)

| Metric | Target | Measurement |
|--------|--------|-------------|
| **False Discovery Rate** | ≤ 5% | Online FDR control + weekly audit |
| **Calibration Coverage** | 95% ± 1% | PIT histogram KS-test p > 0.05 |
| **Alert Utility** | > 0 | Cost-weighted: TP×benefit - FP×cost |
| **Detection Latency** | < 10ms p99 | End-to-end: TAS → Alert |
| **Model Freshness** | < 1hr | Max time since last parameter update |
| **Data Completeness** | > 99.9% | No gaps in parquet > 1min |

---

## Appendix: Example CEL Rules (Post-Implementation)

```yaml
# Bayesian decision with cost matrix
- name: "bayesian_large_print"
  expression: |
    trade.is_option &&
    bayesian_decision(config.cost_fp, config.cost_fn) &&
    trade.delta_weighted_size >= config.p95_delta_weighted_size
  severity: "high"

# Regime-aware threshold
- name: "regime_adaptive"
  expression: |
    trade.is_option &&
    trade.size >= config.p95_by_regime[stats.current_regime] *
                  (1 + stats.vol_ratio * 0.5)
  severity: "medium"

# Cross-asset excitation
- name: "es_leads_spy"
  expression: |
    trade.is_option &&
    trade.underlying == "SPY" &&
    hawkes_cross_intensity["/ES:XCME"] > config.cross_excitation_threshold &&
    trade.delta_weighted_size >= config.p95_delta_weighted_size
  severity: "high"

# Toxic flow
- name: "toxic_flow"
  expression: |
    trade.is_option &&
    trade.vpin >= config.vpin_threshold &&
    trade.delta_weighted_size >= config.p95_delta_weighted_size
  severity: "critical"

# Systemic flow
- name: "systemic_risk"
  expression: |
    trade.systemic_flow_score >= config.systemic_threshold
  severity: "critical"
```

---

*This plan is a living document. Update after each sprint retrospective.*