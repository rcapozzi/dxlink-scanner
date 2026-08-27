# Model Cards — Statistical Models in DXLink Scanner

This document describes each statistical model used in the scanner: intent, assumptions, limitations, and monitoring plan.

---

## 1. BayesianGammaPoisson

**Intent**: Online anomaly detection for trade counts and sizes per symbol. Estimates a posterior rate parameter λ from observed trade counts, and computes p-values / Bayes factors for incoming trades.

**Assumptions**:
- Trade arrivals follow a Poisson process with rate λ.
- Prior on λ is Gamma(α, β).
- Conjugate updating: α_post = α + Σxᵢ, β_post = β + n.

**Limitations**:
- Assumes time-homogeneous rate (no intraday seasonality — handled separately by `TimeOfDaySeasonality`).
- Does not model clustering/burstiness (handled by `HawkesProcess`).

**Calibration**: PIT values should be uniform on [0, 1]; coverage of 95% credible intervals should be ≈ 95%.

**Monitoring**: `ModelHealthSnapshot.pit_uniformity_pvalue`, `coverage_rate`, `alpha_drift`, `beta_drift`.

**Code**: `src/dxlink_scanner/stats/statistical_analysis.py`

---

## 2. HawkesProcess

**Intent**: Detect self- and cross-excitation in trade arrival times. Alerts when intensity deviates from baseline, indicating information-driven trading activity.

**Assumptions**:
- Events follow a Hawkes process with intensity λ(t) = μ + α Σᵢ φ(t − tᵢ).
- Excitation kernel φ(τ) = β · e^(−βτ) (exponential decay).

**Limitations**:
- Exponential kernel is a simplification; real markets may have power-law decay.
- Single-parameter excitation (α) doesn't distinguish buy vs. sell-initiated events.

**Calibration**: Hawkes residuals (compensator-transformed inter-event times) should follow Exp(1) under null.

**Monitoring**: `hawkes_intensity`, `hawkes_mu` in `ModelHealthSnapshot`.

**Code**: `src/dxlink_scanner/stats/statistical_analysis.py`

---

## 3. TimeOfDaySeasonality

**Intent**: Model intraday volume seasonality across RTH (Regular Trading Hours) and ETH (Extended Hours) sessions.

**Assumptions**:
- Volume distribution per time bin is approximately stationary across days.
- RTH and ETH seasonality patterns are independent.

**Limitations**:
- Does not capture day-of-week or holiday effects.
- Assumes seasonality pattern is stable; regime transitions may invalidate this.

**Calibration**: Rolling correlation of bin means across weeks; alert if pattern shifts > 2σ.

**Monitoring**: `seasonality_factor`, `seasonality_expected_volume` in CEL activation.

**Code**: `src/dxlink_scanner/stats/statistical_analysis.py`

---

## 4. CrossSymbolPool

**Intent**: Empirical Bayes shrinkage — share information across related symbols (e.g., SPY/QQQ/SPX options) to improve estimates for sparse symbols.

**Assumptions**:
- Symbols share a common prior hyperparameter structure.
- Exchangeability: symbols can be pooled without loss of information.

**Limitations**:
- Pooling assumes similarity; structurally different symbols (e.g., ES vs. SPY) may have different distributions.
- Shrinkage reduces false positives but may mask true regime differences.

**Calibration**: Pooled CI coverage should match nominal rate across the pool.

**Monitoring**: `bayesian_pooled_mean`, `bayesian_pooled_ci_low/high` in CEL; tracked via `ModelHealthSnapshot`.

**Code**: `src/dxlink_scanner/stats/statistical_analysis.py`

---

## 5. VolumeAtPrice

**Intent**: Build a real-time VAP (Volume-at-Price) profile to identify the Point of Control (POC), value area, and order flow imbalance.

**Assumptions**:
- Trade prices cluster around meaningful price levels.
- Volume at price levels reflects resting liquidity.

**Limitations**:
- Does not include off-exchange or hidden liquidity.
- Tick size quantization affects price level binning.

**Calibration**: POC should align with price levels showing high trade concentration over subsequent periods.

**Monitoring**: `vap_poc`, `vap_val_area_low/high`, `vap_imbalance` in snapshot and CEL.

**Code**: `src/dxlink_scanner/stats/statistical_analysis.py`

---

## 6. RegimeDetector

**Intent**: Classify market state into discrete regimes (low_vol, normal, high_vol, crash) based on realized volatility and volume patterns.

**Assumptions**:
- Volatility clusters persist for meaningful periods.
- Thresholds `vol_low`, `vol_high`, `vol_crash` adequately separate regimes.

**Limitations**:
- Volatility is a proxy for regime; fundamental regime changes may not be captured.
- Lookback window (`vol_window`) determines sensitivity to regime transitions.

**Calibration**: Regime classifications should align with known market events (FOMC, CPI, etc.).

**Monitoring**: `regime`, `regime_probability`, `regime_volatility` in `ModelHealthSnapshot`.

**Code**: `src/dxlink_scanner/stats/statistical_analysis.py`

---

## 7. CrossAssetHawkes (Sprint 6)

**Intent**: Multivariate Hawkes process modeling cross-asset excitation (e.g., ES futures trades excite SPY options intensity).

**Assumptions**:
- Cross-asset excitation is symmetric and uniform (simplified).
- Exponential decay kernel applies to cross-asset effects.

**Limitations**:
- Single excitation parameter for all pairs; real cross-asset relationships are heterogeneous.
- Does not model lead-lag explicitly; uses lag-1 autocorrelation for lead-lag detection.

**Calibration**: Cross-asset intensity should spike during known cross-market events.

**Monitoring**: `systemic_score`, `cross_asset_vpin` in CEL; `ModelHealthSnapshot.regime` per symbol.

**Code**: `src/dxlink_scanner/stats/microstructure.py`

---

## Bayesian Decision Framework

**Intent**: Make alert decisions based on expected cost, not arbitrary thresholds.

**Formula**: Alert iff `BF × (cost_FN / cost_FP) > (1 - π) / π`, where BF is the Bayes factor and π is the prior probability of anomaly.

**Defaults**: cost_FP = 1.0, cost_FN = 10.0, cost_missed_regime_shift = 50.0.

**Monitoring**: `posterior_mean`, `bayes_factor`, `p_value`, `decision_threshold`, `alert_utility` in Alert records.

**Code**: `src/dxlink_scanner/stats/model_store.py` (`bayesian_decision()` function)

---

## Online FDR (SAFFRON/LORD)

**Intent**: Control false discovery rate in streaming alert decisions without knowing the total number of tests in advance.

**Method**: LORD (Leveraging Online Randomization) procedure with parameters:
- `alpha`: Target FDR level (default: 0.05)
- `alpha_t`: Threshold parameter for SAFFRON (default: 0.05)
- `lambda`: Tuning parameter for initial threshold (default: 0.05)
- `tau`: SAFFRON tuning parameter (default: 0.5)
- `pi0`: Proportion of nulls (estimated online, default: 1.0)

**Limitations**: FDR control assumes independence or positive dependence; strong negative dependence may violate guarantees.

**Monitoring**: `stats.fdr_threshold`, `config.fdr_alpha` in CEL.

**Code**: `src/dxlink_scanner/stats/model_store.py` (`online_fdr_threshold()` function)
