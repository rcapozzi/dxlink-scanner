# Statistical Enlightenment Plan — DXLink Scanner

> **Objective**: Evolve the scanner from a rule-based alerting system into a statistically rigorous, data-informed platform where every alert, threshold, and decision is grounded in proper statistical inference. Data collection feeds model updating; models inform real-time decisions; decisions generate new data. A closed loop.

---

## Current State (Sprint 0 Complete)

| Component | Status | Notes |
|-----------|--------|-------|
| **Core Pipeline** | ✅ | Quote/TAS/TheoPrice → ConsolidatedSnapshot → CEL engine → Alerts |
| **TheoPrice Greeks** | ✅ | delta, gamma, dividend, interest in snapshot & parquet |
| **Delta-weighted size** | ✅ | `trade.delta_weighted_size` in CEL |
| **P95 Significance Thresholds** | ✅ | Nightly compaction computes per-symbol P95 |
| **Statistical Module** | ✅ | Bayesian Gamma-Poisson, Hawkes, Seasonality, Cross-symbol pooling, VAP, Regime detector |
| **CEL Integration** | ✅ | Bayesian posterior, Hawkes intensity, seasonality factors exposed in `config` context |
| **Tests** | ✅ | 162 passing, 72% coverage |

---

## Phase 1: Statistical Foundations (Sprints 1–2)

### Sprint 1: Bayesian Online Learning & Model Persistence (2 weeks)

**Goal**: Make Bayesian models persistent across restarts; enable proper prior updating from historical data.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 1.1 | **Model serialization** — Add `to_dict()`/`from_dict()` to `BayesianGammaPoisson`, `HawkesProcess`, `TimeOfDaySeasonality`, `CrossSymbolPool`, `VolumeAtPrice`, `RegimeDetector` | JSON-serializable model state |
| 1.2 | **Startup warm-up** — Load `significance_meta.json` + new `models_meta.json` on scanner start; initialize posteriors from historical data | Models start with informed priors, not defaults |
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

**New Metrics in Parquet** (schema v3):
- `bayesian_log_pred_lik`
- `hawkes_log_pred_lik`
- `pit_value`
- `seasonality_correlation`

**Acceptance**: All calibration metrics in CI; model quality tracked per symbol per day.

---

## Phase 2: Decision-Theoretic Alerting (Sprints 3–4)

### Sprint 3: Cost-Aware Alerting & FDR Control (2 weeks)

**Goal**: Replace arbitrary thresholds with decision-theoretic rules controlling false discovery rate.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 3.1 | **Cost matrix configuration** — YAML: `cost_false_positive`, `cost_false_negative`, `cost_missed_regime_shift` per severity | `alert_costs.yaml` |
| 3.2 | **Bayesian decision rule** — Alert iff `P(H1|data) * cost_FN > P(H0|data) * cost_FP` | New CEL function `should_alert(cost_fp, cost_fn)` |
| 3.3 | **Online FDR (SAFFRON/LORD)** — Replace static BH with online FDR for streaming alerts | `stats.fdr_threshold` in CEL |
| 3.3 | **Multiplicity correction across symbols** — Hierarchical FDR: control FDR at underlying level, then option level | `config.fdr_underlying`, `config.fdr_symbol` |
| 3.5 | **Alert quality logging** — Every alert logs: posterior prob, Bayes factor, decision threshold, cost-weighted utility | Parquet field `alert_utility` |

**New CEL Functions**:
```cel
bayesian_decision(cost_fp, cost_fn) -> bool
online_fdr_pvalue(pval) -> bool
hierarchical_fdr(symbol_pvals) -> bool
```

**Acceptance**: Simulated backtest shows FDR ≤ 5% with 2× detection power vs fixed thresholds.

---

### Sprint 4: Regime-Aware & Adaptive Thresholds (2 weeks)

**Goal**: Thresholds adapt to market regime; alerts context-aware.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 4.1 | **Regime-conditioned thresholds** — P95 thresholds computed separately per regime (low_vol, normal, high_vol, crash) | `significance_meta.json` now has `regime_thresholds` |
| 4.2 | **Real-time regime probability** — `RegimeDetector` outputs `P(regime=r)`; used to blend thresholds | `config.regime_probs` map in CEL |
| 4.3 | **Adaptive window sizing** — RollingStatsV2 window expands in low-vol, contracts in high-vol (volatility-scaled) | `stats.effective_window` |
| 4.4 | **Volatility targeting** — Alert thresholds scale with realized vol: `threshold = base * (vol_target / current_vol)` | `config.vol_adjusted_threshold` |
| 4.5 | **Regime transition alerts** — Hawkes intensity spike + regime prob shift → "regime_change" alert | New alert type `REGIME_SHIFT` |

**New CEL Variables**:
```cel
config.regime_probs          // {0: 0.1, 1: 0.7, 2: 0.2, 3: 0.0}
config.p95_by_regime         // {0: 50, 1: 100, 2: 200, 3: 500}
stats.vol_ratio              // current_vol / target_vol
```

**Acceptance**: In high-vol regime, alert rate per true anomaly increases; false alerts don't explode.

---

## Phase 3: Advanced Analytics & Order Flow (Sprints 5–6)

### Sprint 5: Volume-at-Price & Microstructure (2 weeks)

**Goal**: Integrate VAP, order flow imbalance, and toxicity metrics into alerting.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 5.1 | **Real-time VAP profile** — `VolumeAtPrice` updated per trade; 70% value area, POC, imbalance in snapshot | `snap.vap_poc`, `snap.vap_val_area_low/high`, `snap.vap_imbalance` |
| 5.2 | **Order flow toxicity (VPIN)** — Volume-synchronized Probability of Informed Trading per Easley et al. | `trade.vpin`, `config.vpin_threshold` |
| 5.3 | **Trade classification** — Lee-Ready / EMO algorithm using bid/ask from Quote + TAS | `trade.side` = "buy"/"sell"/"unknown" |
| 5.4 | **Flow toxicity alerts** — Alert when VPIN > threshold AND delta-weighted size large | New rule template `toxic_flow` |
| 5.5 | **Liquidity metrics** — Bid-ask spread percentile, depth at POC, spread persistence | `stats.spread_p95`, `stats.depth_at_poc` |

**Parquet Additions** (v3):
- `vap_poc`, `vap_val_area_low`, `vap_val_area_high`, `vap_imbalance`
- `trade_side`, `vpin`, `spread_bps`

**Acceptance**: VPIN correlates with subsequent 5-min price moves (|corr| > 0.3) in backtest.

---

### Sprint 6: Cross-Asset & Multi-Timeframe (2 weeks)

**Goal**: Joint modeling across underlyings and timeframes.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 6.1 | **Cross-asset Hawkes** — Multivariate Hawkes: SPY trades excite QQQ intensity, ES excites SPX | `hawkes_cross_intensity["SPY->QQQ"]` |
| 6.2 | **Lead-lag detection** — Granger causality / transfer entropy on 1-min bucketed volume | `config.lead_lag["SPY->QQQ"]` |
| 6.3 | **Multi-timeframe thresholds** — Combine 1-min, 5-min, 15-min stats: `threshold = max(p95_1m, 0.8*p95_5m, 0.6*p95_15m)` | `config.multi_tf_p95` |
| 6.3 | **Correlation risk** — Rolling correlation of option flow across symbols; alert on correlation breakdown | `stats.flow_correlation` |
| 6.5 | **Systemic risk index** — PCA on cross-symbol volume residuals; first PC = systemic flow | `config.systemic_flow_index` |

**New CEL**:
```cel
trade.systemic_flow_score > config.systemic_threshold
hawkes_cross_intensity[symbol] > config.cross_excitation_threshold
```

**Acceptance**: Cross-asset model detects 15-min lead of ES flow on SPY options in backtest.

---

## Phase 4: Production Hardening & Observability (Sprints 7–8)

### Sprint 7: Reliability & Data Quality (2 weeks)

| Task | Description |
|------|-------------|
| 7.1 | **Data quality monitors** — Gap detection in parquet (missing timestamps), schema drift alerts, outlier detection in model params |
| 7.2 | **Model health endpoint** — `/health/models` returns: calibration PIT, coverage, last update, parameter drift |
| 7.3 | **Replay framework** — CLI command to replay parquet through statistical models + CEL for backtesting |
| 7.4 | **A/B testing infrastructure** — Run two rule configs in parallel (shadow mode); compare alert quality |
| 7.5 | **Documentation** — Statistical model cards for each model (intent, assumptions, limitations, calibration) |

---

### Sprint 8: Performance & Scale (2 weeks)

| Task | Description |
|------|-------------|
| 8.1 | **Vectorized model updates** — Batch Bayesian/Hawkes updates per symbol using NumPy (10-100× speedup) |
| 8.2 | **Memory optimization** — Ring buffers for Hawkes event history; compress old events via sufficient statistics |
| 8.3 | **Parallel compaction** — Multi-process parquet compaction + significance computation |
| 8.4 | **Benchmark suite** — Latency percentiles (p50/p99) for: model update, CEL eval, end-to-end alert |
| 8.5 | **Scale test** — 500 symbols, 100K events/sec; verify < 10ms p99 latency |

---

## Cross-Cutting Concerns (Continuous)

| Concern | Implementation |
|---------|----------------|
| **Reproducibility** | All random seeds fixed; model versions pinned; data lineage in parquet metadata |
| **Auditability** | Every alert includes: model state snapshot, decision trace, cost-benefit calc |
| **Privacy** | No PII; all data is market data |
| **Regulatory** | Model cards document: intended use, limitations, bias assessment, monitoring plan |

---

## Sprint Schedule Summary

| Sprint | Theme | Duration | Key Deliverable |
|--------|-------|----------|-----------------|
| 1 | Bayesian Persistence & Pooling | 2 wks | Models survive restart; sparse symbols pooled |
| 2 | Calibration & Validation | 2 wks | PIT histograms, coverage tests, model comparison |
| 3 | Decision-Theoretic Alerting | 2 wks | Cost-aware rules, online FDR |
| 4 | Regime-Adaptive Thresholds | 2 wks | Regime-conditioned P95, vol-targeting |
| 5 | Microstructure & VAP | 2 wks | VPIN, toxicity, flow classification |
| 6 | Cross-Asset & Multi-TF | 2 wks | Multivariate Hawkes, lead-lag, systemic index |
| 7 | Reliability & Replay | 2 wks | Health endpoints, replay CLI, A/B framework |
| 8 | Performance & Scale | 2 wks | Vectorized updates, 100K eps benchmark |

**Total**: 16 weeks (4 months) to statistically enlightened production system.

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