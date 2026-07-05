# Frozen Constants Manifest

One-page inventory of every frozen decision constant (plan_phase3.md §B5 "Config
integrity"). Doubles as the **±25% sensitivity-sweep checklist** (B5b) and the **DSR
trials-count checklist** (B7a). Every constant here is frozen per WF window; changing a
constant is a spec change that restarts the WF cycle. `settings_hash` (config/integrity.py)
hashes the decision-relevant subset and is stamped on every trade; a mid-window change
raises `[CONFIG_DRIFT]`.

## Phase 1 — Bayesian core
| Constant | Value | Role |
|---|---|---|
| BAYES_ALPHA0 / BAYES_BETA0 | 3.0 / 3.0 | Beta(3,3) prior |
| BAYES_DECAY | 0.999 | evidence decay per update |
| WINSOR_MAX_LOSS_R | −1.5 | posterior-score loss floor |
| SHRINK_K | 30 | model-uncertainty shrinkage |
| EV_GATE_LOW / EV_GATE_HIGH | 0.15 / 0.25 | EV soft ramp |
| DRIVER_MU_LOW / DRIVER_MU_HIGH | 0.52 / 0.58 | driver soft ramp |
| EFF_CLUSTER_MIN | 1.5 | dual cluster gate |
| MAX_CONTRADICTING | 1 | contradiction gate (raw) |
| CONFIRM_WINDOW_MIN | 30 | contemporaneous confirmation window |
| VOTE_C_FLOOR / VOTE_C_SCALE | 0.3 / 0.15 | confidence-vote weighting |
| KELLY_FRACTION | 0.10 | fractional Kelly |
| MAX_RISK_PER_TRADE | 0.5% | per-trade cap |
| MAX_DAILY_RISK | 0.8% | per-day cap |
| MAX_STOCK_NOTIONAL / MIS_LEVERAGE | 100% cash / 5× | notional caps |
| SEBI_MARGIN_FLOOR | 20% | margin check |
| LIQUIDITY_ADV_CAP | 1% of 20d ADV | liquidity cap |
| ROUND_RISK_TOLERANCE | 25% | integer-round skip |
| BAYES_BURN_IN_NEFF / BURN_IN_RISK_FRACTION | 20 / 0.05% | cold-start burn-in |
| SLIPPAGE_BPS / SLIPPAGE_IMPACT_K | 5 bps / 1.0 | baseline fills |
| LAST_ENTRY_TIME / EOD_SQUAREOFF_TIME | 14:30 / 15:10 | time filters |
| EVENT_DAY_MODE / EVENT_MIN_EV / EVENT_MIN_CLUSTERS | SKIP / 0.35 / 3 | macro filter |

## Phase 2 — enrichment
| Constant | Value | Role |
|---|---|---|
| REGIME_MIN_NEFF | 20 | regime-bucket fallback |
| VIX_R1_PCTILE / VIX_BAND_LO/HI_PCTILE | 80 / 75 / 85 | regime VIX threshold + blend band |
| ADX_THRESHOLD / ADX_BAND | 25 / 2 | regime ADX threshold + blend |
| CRASH_NIFTY_RET | −2% | R4 crash |
| HYSTERESIS_DAYS | 3 | regime commit buffer |
| STOCK_TYPE_ALPHA0/BETA0 / STOCK_TYPE_MIN_NEFF | 5 / 5 / 15 | per-stock prior |
| K_HIER | 25 | hierarchical shrinkage |
| CHANGEPOINT_HAZARD / CHANGEPOINT_ALARM / CHANGEPOINT_TEMPER | 1/200 / 0.70 / 0.5 | change-point (defensive overlay) |
| EXEC_CHASE_VETO_ATR | 4.0 | the one hard veto |
| EXEC_SKIP_FLOOR | 0.25 | exec-mult skip |
| EXEC_MS_ACCEPT_ATR/CLOSES | 0.3 / 2 | structure acceptance |
| EXEC_EE_EXT_HI / EE_VWAP_LO/HI / EE_CONSEC_LO/HI | 4 / 4 / 8 / 4 / 8 | entry efficiency ramps |
| BREADTH_LONG_MIN / BREADTH_SHORT_MAX / CONTEXT_MULT_OPPOSED | 0.30 / 0.70 / 0.7 | breadth rule |
| SIGNAL_LABEL_ATR_MULT / SIGNAL_LABEL_BARS | 0.5 / 12 | signal outcome label |

## Phase 3 — validation & defensive overlays
| Constant | Value | Role |
|---|---|---|
| DAILY_LOSS_HALT | 1.2 × MAX_DAILY_RISK (0.96%) | daily halt |
| MONTHLY_LOSS_HALT | 4% | monthly halt |
| ECE_TARGET / BRIER_TARGET / EV_REALISATION_MIN | 0.05 / 0.22 / 0.50 | calibration gates |
| PROB_DRIFT_WINDOW / PROB_DRIFT_ALARM | 30 / 0.10 | drift alarm |
| DSR_CONFIDENCE / PBO_GATE | 0.95 / 0.20 | overfitting gates |
| MC_SIMS / MC_DD_GATE / MC_DD_PROB / MC_NEGYEAR_PROB | 10000 / 15% / 5% / 10% | Monte Carlo gates |
| STRESS_DD_MULT / SENSITIVITY_PCT | 1.5 / 25% | stress gates |
| RISK_SCALE_MULTIPLIERS | 1.5/2.0/2.5 | B7e sweep (study only; baseline caps go live) |

## Set-valued choices (pre-registered; NOT swept — changing them restarts the WF cycle)
- Cluster assignments (config/strategy_clusters.json) and the inter-cluster matrix C
- The B4e level menu and component applicability map
- Correlation-cap pairs (r > 0.70)
- The 53-strategy library membership and driver-eligibility (E/F not driver-eligible)
