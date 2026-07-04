# Phase 3 — Validation & Walk-Forward
**Build steps: B5 (Calibration + Drift Monitoring) → B5b (Execution Simulator + Stress Tests) → B6 (Full Walk-Forward Run) → B7 (Statistical Robustness Suite) → B8 (Benchmarks + Capacity)**
**Requires Phase 1 and Phase 2 exit criteria to be fully passed before starting.**

---

## CONTEXT: WHAT PHASES 1 AND 2 DELIVERED

After Phase 1 + 2, the system has:
- Beta posteriors per strategy × direction × regime (48 strategies, 5 regime buckets)
- Four-gate entry filter: eff_weighted ≥ 1.5 AND eff_binary ≥ 1.5 (confidence-weighted,
  dependency-penalised), contradicting ≤ 1, driver_mu soft ramp 0.52→0.58,
  EV soft ramp 0.15→0.25 (on shrunk P(win))
- Hierarchical cluster priors (strategies share strength within a family)
- Bayesian change-point detection with posterior tempering on alarm
- Regime detection with hysteresis buffer and boundary blending
- Per-stock behavior prior (trend vs reversion modifier per stock)
- 0.10-Kelly position sizing with portfolio caps (0.5%/trade, 0.8%/day, 100%-of-cash
  stock notional, margin check, sector separation, 1%-of-ADV liquidity cap)
- Realistic fills: next-bar open + slippage model (baseline; full simulator in B5b)
- Execution quality layer (B4e): structure-acceptance, entry-efficiency, and
  candle-quality size modifiers (momentum quality log-only); single chase hard veto;
  `[CF_EXEC]` counterfactual outcome logged for every cut
- Context layer v1 (B4f): day-type / breadth / sector / daily-trend / time-bucket
  tags on every trade; one breadth-opposition sizing rule
- Confidence-weighted cluster confirmation with dual gate (eff_weighted AND
  eff_binary); `[CF_CONTRA]` counterfactual log on the contradiction gate
- All 48 strategies active and accumulating evidence

Phase 3 validates the entire system against 7+ years of out-of-sample data using a
strict walk-forward protocol. No parameter changes after the first WF freeze.

---

## 1. BACKTESTING PROTOCOL

### 1a. Walk-Forward Structure

```
WF-1: Train 2016-2018  →  FREEZE wf1_bayes.json  →  Test 2019
WF-2: Train 2016-2019* →  FREEZE wf2_bayes.json  →  Test 2020  (COVID)
WF-3: Train 2016-2020* →  FREEZE wf3_bayes.json  →  Test 2021
WF-4: Train 2016-2021* →  FREEZE wf4_bayes.json  →  Test 2022  (bear year)
WF-5: Train 2016-2022* →  FREEZE wf5_bayes.json  →  Test 2023
WF-6: Train 2016-2023* →  FREEZE wf6_bayes.json  →  Test 2024
WF-7: Train 2016-2024* →  FREEZE wf7_bayes.json  →  Test 2025
WF-8: Train 2016-2025* →  FREEZE wf8_bayes.json  →  Test 2026  ← LIVE

* posteriors update from test outcomes inline — no separate re-run of test years
```

OOS starts 2019. 7+ years of out-of-sample data vs current system's 3 years (2023–2026).

**Key rule:** decisions in each test year are made using only the frozen weights from the
preceding freeze. The posterior continues to update from test outcomes so the next freeze
captures real evidence, but those updates do not affect decisions in the current test year.

**Defensive-overlay exception (the only exception to the freeze):** the change-point
tempering (Phase 2 B3d) and the loss-threshold halts (B5) DO act within the test year —
they may reduce position size or halt new entries, never increase size and never admit
a trade the frozen weights would reject. Their governing constants (hazard prior,
CHANGEPOINT_ALARM, halt thresholds) are frozen like every other parameter, and the
identical mechanism runs live. Without this exception, B3d's "size drops within 5
trades of alarm" would be impossible and a live edge-death response would be deferred
to the next annual freeze. Reduction-only bounds the leakage: reacting to test-year
data can only make the backtest MORE conservative in exposure, never mine test data
for extra edge. (B4e counterfactual promotions and §2g contradiction-rule changes
remain annual-cycle only — they are not defensive and get no exception.)

**New strategies in the WF run:** all 16 new strategies start at Beta(3,3) in 2016 and
accumulate evidence during training years like existing strategies. By the WF-1 freeze
(end 2018), they have 3 years of evidence and meaningful posteriors.

### 1b. Bayesian-Specific Metrics (computed per WF window)

**1. Expected Calibration Error (ECE)**
Bin trades by P(win): [40-50%, 50-55%, 55-60%, 60-65%, 65%+].
```
ECE = Σ |predicted_wr − actual_wr| × (n_bin / n_total)
```
Target: ECE < 0.05. Measures whether the probability estimates are trustworthy.

**2. Brier Score**
```
Brier = (1/N) × Σ (P(win) − actual_outcome)²
```
Target: < 0.22 (random baseline = 0.25). Lower = better probability estimates.

**3. Posterior width convergence**
Track 95% CI width per strategy at start vs end of each test window.
Shrinking = learning from real outcomes. Widening = regime shift or stale evidence.

**4. EV realisation rate**
Compare predicted EV vs actual PnL/risk_amount.
If EV=0.6 consistently realises 0.3 → model is overconfident; recalibrate prior.

**5. Probability drift (monthly, not yearly)**
Rolling 30-trade window: |mean predicted P(win) − realised win rate|.
Alarm if gap > 10 percentage points. Yearly calibration is too slow to catch a
mid-year break — this is the fast-loop version of the ECE check.

**6. Strategy importance report (monthly)**
EV contribution and realised PnL contribution per strategy per month.
Flags: strategy with n_eff > 40 contributing < 0 realised R over trailing 6 months →
candidate for review. Otherwise dead indicators stay in the library forever.

### 1c. Walk-Forward Gates

Existing performance gates unchanged. Additional Bayesian gates:

| Gate | Requirement |
|---|---|
| Calibration | ECE < 0.05 in at least 4 of 5 WF windows |
| Brier Score | < 0.22 in WF-5 (2023 live-period proxy) |
| EV realisation | Realised PnL/risk ≥ 50% of predicted EV in WF-5 |
| Regime posteriors | R1/R2/R3 posterior means differ by > 5% in at least 3 strategies |

---

## 2. BUILD STEPS

### B5 — Calibration Reporting

**Files:**
- `reports/calibration.py`
  - ECE computation: bins trades by P(win) bucket, compares predicted vs actual win rates
  - Brier Score per WF window
  - EV realisation rate: predicted EV vs actual pnl/risk_amount per trade
  - Posterior width convergence table per strategy per window
  - **Probability drift alarm** — rolling 30-trade |predicted − realised| win-rate gap;
    logs `[PROB_DRIFT predicted=0.63 realised=0.45 gap=0.18]` when gap > 0.10;
    evaluated monthly, not yearly
  - **Strategy importance report** — monthly EV and realised-PnL contribution per
    strategy; flags established strategies (n_eff > 40) with negative trailing-6-month
    realised contribution
  - **Counterfactual cut report** — monthly realized-R of the sets removed by each
    exec-quality component (`[CF_EXEC]`, B4e) and by the contradiction gate
    (`[CF_CONTRA]`, Phase 1 §2g). This is the pre-registered promotion/demotion
    evidence; it is REVIEWED monthly and ACTED ON only at annual WF cycles
  - **Tag breakdowns** — monthly performance by time bucket, day type, and breadth
    band (log-only dimensions from B4f; no thresholds attached to them)
  - **Loss-decomposition & win-capture report** — monthly, from the B2 excursion
    record: losing trades classified never-worked (straight to stop) / gave-back
    (MFE > 1R before the stop) / truncated (EOD with positive MFE), per strategy ×
    regime — the aggregate "which lens failed" answer. Winners report the capture
    ratio (realized_R / MFE_R) and median winner MFE vs target (are frozen targets
    leaving money?). Report-only; this is the evidence base for the §2c future exit
    project, acted on at annual cycles only
  - **Config integrity** — settings.py is hashed at session start and stamped on
    every trade record (settings_hash, B2); any mid-window hash change raises a
    `[CONFIG_DRIFT]` flag in this report. `config/settings_manifest.md` inventories
    every frozen constant on one page and doubles as the sensitivity-sweep
    checklist — freeze discipline as an audit trail, not a promise
- Hooked into `generate_report.py` — runs automatically after each test year completes
  (drift alarm and importance report run monthly within the year)
- New section in `memory/strategy_agent.md`: "Bayesian Calibration per WF Window"

No backtesting dependency. Reads `paper_trades.csv`. Can run at any time on existing logs.

**Loss-Threshold Halt & Approval Gate (built as part of B5):**

Execution is fully automated — entries and exits never wait for a human. The single
human touchpoint in the whole system is this gate: when losses cross a hard threshold,
the system halts itself and will not trade the next session until explicitly approved.

```
DAILY_LOSS_HALT   = 1.2 × MAX_DAILY_RISK of capital     (default 0.96%; the 0.8%
                    daily risk cap bounds losses by construction, so crossing this
                    means slippage/circuit-trap overruns — a red flag by definition)
MONTHLY_LOSS_HALT = 4% of capital, rolling calendar month  (settings.py)

Triggers (either condition, evaluated after every trade settlement):
  1. Realized day PnL   < −DAILY_LOSS_HALT
  2. Realized month PnL < −MONTHLY_LOSS_HALT

Action (automatic, no discretion):
  - Halt all NEW entries immediately; protective exits and EOD square-off
    continue to run
  - Log [LOSS_HALT daily|monthly pnl=−X] and send alert
  - Next session, the agent starts in HALTED state and refuses to trade until
    explicitly approved (e.g. `--approve-resume` flag / signed config entry);
    the approval event is logged with timestamp
  - Monthly halt: approval re-arms trading but the monthly counter keeps
    accumulating — a second monthly trigger in the same month halts again

Advisory (log-only, feeds the approval decision — never blocks on its own):
  - N consecutive losing trades ≥ 3
  - Rolling 5-day PnL < −(2 × avg_risk_per_trade)
  - Active [CHANGEPOINT] or [PROB_DRIFT] alarms are surfaced in the halt report
    so the approval decision is informed, not blind
```

**Implementation:**
- `reports/calibration.py` — `check_loss_halt(trades_df)` runs after every trade
  settlement; writes halt state to `checkpoints/halt_state.json` (survives restart —
  a crash cannot silently clear a halt)
- The Phase 4a kill switch and this gate share one mechanism: halted = no new
  entries, exits alive
- Threshold values in `settings.py`

**Validation:** Run against 2020 COVID period and 2022 bear year in the WF run.
Confirm the halt would have triggered during the worst weeks and count approval
events per year — more than ~6/year means thresholds are too tight and the gate
becomes noise. Confirm zero halts during good months. Confirm halt state survives
an agent restart (kill mid-halt, relaunch, still HALTED).

**Validation:** Run against 2016–2018 training data first.
- ECE should be near 0 on training data (system was trained on it — sanity check only).
- Brier Score on training data confirms formula is correct before OOS test.

---

### B5b — Execution Simulator + Reality Stress Tests

Phase 1 B2 introduced baseline realism (next-bar-open fills + flat slippage model).
Before the full WF run, upgrade to a proper execution simulator — Nifty 500 contains
genuinely illiquid names where the baseline model is too kind.

**Execution simulator (`backtester/execution_sim.py`):**
```
Per fill, model:
  - bid-ask spread estimate      : from bar range / stock liquidity tier
                                   (wider at open, on event days, in smallcaps)
  - slippage vs liquidity        : impact ∝ order_size / bar_volume (participation rate)
  - latency                      : signal at bar close → order reaches market next bar
  - partial fills                : if order > 20% of bar volume, fill the remainder at
                                   the following bar's price or cancel (configurable)
  - gap-through stops            : fill at bar open, never at stop price
  - circuit locks                : see below — a locked band means NO fill, not a bad fill
```
The LIQUIDITY_CAP from Phase 1 sizing (≤ 1% of 20-day ADV) keeps most fills clean;
the simulator is what proves it.

**Circuit-lock model — a locked band has no counterparty:**

"Fill at bar open" still assumes a fill exists. At a locked circuit there are zero
sellers (upper) or buyers (lower): the exit order simply does not execute, an MIS
square-off fails, and a trapped short goes to exchange auction with penalty. The
Phase 1 universe filter (shorts F&O-only) removes most of the catastrophic
trapped-short tail — but F&O dynamic bands also *pin*: price can sit at the band edge
through the 15-minute flex cool-off. No-fill-while-pinned is modeled for all stocks:

```
Detection : bar closes at the band price with H == L (or volume collapse at band)
Behaviour : exit order does NOT fill; re-attempt each subsequent bar
Still locked at 15:15:
  LONG  → force-sell at next day's open − slippage (forced overnight carry)
  SHORT → simulated auction settlement: next-day open + max(20% penalty band,
          actual adverse move)
Log: [CIRCUIT_TRAP symbol=X side=SHORT entry_day_locked → auction_cost=−4.2R]
```

Circuit-trap losses feed PnL, drawdown gates, and Monte Carlo **uncapped** — the
−1.5R winsorization floor applies only to the posterior evidence score (Phase 1 §2c
already separates real PnL from Bayesian evidence).

**Slippage calibration study (replaces the printed-open assumption without repiping
the backtest to 1-min data):**

The first seconds of a 5-min bar in mid-caps are erratic — clustered algo orders
create a synthetic bid-ask bounce, so the printed open is an optimistic anchor.
Kite Connect serves 1-min candles (back to ~2015, 60-day chunks, rate-limited), but
repiping 500 stocks × 10 years is a huge second dataset for a marginal gain. Instead:

```
1. Download 1-min data for a stratified sample: ~50 stocks × 6 months,
   spread across liquidity tiers (large / mid / small-cap)
2. Measure the distribution of (first-minute VWAP − printed 5-min open) per tier
3. Set the simulator's per-tier slippage constants from the measured distribution
   (replacing the flat 5 bps default from Phase 1 B2)
4. Fallback if 1-min unavailable for a name: entry = open + conservative
   tier-dependent adverse offset
```

**Stress scenarios — the full WF run (B6) is re-executed under each:**

| Scenario | Perturbation |
|---|---|
| S1 | 2× slippage on every fill |
| S2 | Miss the first qualifying trade each day |
| S3 | 10% of signals randomly dropped (seeded) |
| S4 | Execution delayed by one full bar on every entry |
| S5 | Transaction costs +50% |
| S6 | Exchange outage: 5 random full days per year removed, open positions force-closed at next open |
| S7 | Circuit worst case: every trade that ever touches a price band is assumed trapped (longs carried overnight, shorts auctioned) |

**Pass bar:** full-period Sharpe stays > 0 and max drawdown stays within 1.5× of the
unstressed run, for every scenario. A strategy that only survives perfect execution
is fragile and does not go live.

**Exec-layer sensitivity (runs alongside S1–S7):** every B4e/B4f scalar constant
(acceptance offset, block_frac anchors, extension ramps, wick ratio, breadth 30/70,
EXEC_SKIP floor) AND the defensive-overlay constants (CHANGEPOINT_ALARM, the BOCPD
hazard prior, both loss-halt thresholds — decision-relevant frozen constants like any
other) is perturbed ±25% — full-period Sharpe must not change sign under any single
perturbation. Set-valued choices (the level menu, the component
applicability map) are pre-registered in B4e and are NOT swept — changing them is a
spec change that restarts the WF cycle.

**Short-universe impact counter (runs throughout B6):** log every SHORT signal that
passed all gates but fell outside the point-in-time F&O universe (Phase 1 filter).
This measures how much short-side opportunity the broker-eligibility constraint
actually costs — the external review claimed ~30% of short fills would be
unexecutable live; this counter replaces that assertion with a measured number.

**Validation:** Run S1–S6 on 2016–2018 training years first to confirm the harness
works before committing to the 19-step WF under stress.

---

### B6 — Full Walk-Forward Run (19 steps)

```
B6-1:  Train 2016 → all 48 strategies; posteriors start at Beta(3,3); update after each trade
B6-2:  Train 2017 → posteriors update (PnL-normalised + decay)
B6-3:  Train 2018 → posteriors update
       ─────────────────────────────────────────────────────────
B6-4:  WF-1 FREEZE → snapshot strategy_bayes.json → wf1_bayes.json
B6-5:  Test 2019   → DECISIONS: frozen wf1_bayes
                     POSTERIOR: updates from 2019 outcomes (inline, no re-run)
       ─────────────────────────────────────────────────────────
B6-6:  WF-2 FREEZE → wf2_bayes.json  (2016-2018 training + 2019 test outcomes)
B6-7:  Test 2020   → DECISIONS: frozen wf2_bayes  [COVID — toughest stress test]
       ─────────────────────────────────────────────────────────
B6-8:  WF-3 FREEZE → wf3_bayes.json  (adds 2020 outcomes)
B6-9:  Test 2021   → DECISIONS: frozen wf3_bayes
       ─────────────────────────────────────────────────────────
B6-10: WF-4 FREEZE → wf4_bayes.json  (adds 2021 outcomes)
B6-11: Test 2022   → DECISIONS: frozen wf4_bayes  [bear year — short-side critical]
       ─────────────────────────────────────────────────────────
B6-12: WF-5 FREEZE → wf5_bayes.json  (adds 2022 outcomes)
B6-13: Test 2023   → DECISIONS: frozen wf5_bayes
       ─────────────────────────────────────────────────────────
B6-14: WF-6 FREEZE → wf6_bayes.json  (adds 2023 outcomes)
B6-15: Test 2024   → DECISIONS: frozen wf6_bayes
       ─────────────────────────────────────────────────────────
B6-16: WF-7 FREEZE → wf7_bayes.json  (adds 2024 outcomes)
B6-17: Test 2025   → DECISIONS: frozen wf7_bayes
       ─────────────────────────────────────────────────────────
B6-18: WF-8 FREEZE → wf8_bayes.json  (adds 2025 outcomes)  ← CURRENT LIVE POSTERIORS
B6-19: Test 2026   → DECISIONS: frozen wf8_bayes → ECE + Brier + P&L per year
```

19 steps. No re-runs. No separate training years for test periods.
Annual WF cycle continues indefinitely after deployment:
freeze at year-end → test next year on frozen weights → repeat.
Training year = one manual trigger (~3–5 hrs). WF freeze = instant snapshot.

---

### B7 — Statistical Robustness Suite (False Discovery + Monte Carlo + CPCV)

With 48 strategies × directions × regimes, thousands of combinations were implicitly
searched. Some will look profitable purely by luck — the classic multiple testing
problem. One good-looking WF equity curve is not proof of edge.

**B7a — False discovery / backtest overfitting control (`reports/overfitting.py`):**
```
1. Deflated Sharpe Ratio (DSR)
   Adjusts the observed Sharpe for the number of strategy configurations effectively
   tried, skewness, and kurtosis of returns.
   Gate: DSR > 0 at 95% confidence.

2. Probability of Backtest Overfitting (PBO) via CSCV
   Split the OOS trade series into 16 blocks; for all combinatorial train/test splits,
   measure how often the in-split best configuration underperforms OOS median.
   Gate: PBO < 0.20.

3. White's Reality Check (bootstrap) as cross-check on the strategy library:
   the full system's returns vs the null of the best random strategy from the
   same universe of tried rules.

4. Configuration accounting: the DSR trials count includes the exec-quality layer's
   constants (B4e), the defensive-overlay constants (CHANGEPOINT_ALARM, hazard prior,
   halt thresholds), and every promotion of any kind — exec components, signal-label
   consumption, stock-type prior, timing posteriors. An aggressively filtered
   execution layer is "configurations tried" even when each filter looks principled,
   and undercounting them makes the deflation too kind.
```

**B7b — Monte Carlo robustness (`reports/montecarlo.py`):**
```
10,000 simulations. Each run perturbs the realised OOS trade sequence with:
  - block bootstrap resampling of trade order (preserves streak structure)
  - random slippage jitter per trade (± the execution simulator's spread estimate)
  - random execution delay (0–1 bar)
  - 5–10% of trades randomly dropped
  - random spread widening episodes

Report the distributions:
  P(max drawdown > 15%)   — gate: < 5%
  P(negative full year)   — gate: < 10%
  5th-percentile Sharpe   — gate: > 0

Circuit-trap losses ([CIRCUIT_TRAP], B5b) enter these simulations at their UNCAPPED
real-PnL values — the winsorization floor protects only the posterior, never the
risk statistics.
```

**B7c — Combinatorial Purged Cross-Validation (CPCV, supplement to WF):**
The walk-forward run is one path through history. CPCV builds many train/test paths
from the same data with purging (embargo gap around each test block so overlapping
positions and decayed posteriors don't leak). Run on the 2019–2026 OOS span,
16 blocks, 2-block test sets, 5-day embargo. Reported alongside the WF result;
gates are informational in this cycle, binding from the next annual cycle.

**B7d — Exec-layer ablation (`reports/ablation.py`):**
```
Arms:
  (0)   full system
  (1)   all-exec-off — the layer's total OOS marginal contribution, measured
        rather than assumed
  (2–4) each sizing-active component (ms, ee, cq) off in isolation
  (5)   contradiction gate: weighted rule enabled (the [CF_CONTRA] set admitted) —
        evaluates the pre-registered §2g loosening without shipping it
  (r)   redundancy arm — drop the exec components most correlated with existing
        cluster votes (ee_vwap vs VWAP-REV, ee_consec vs RSI-EXT, mq-volume vs
        VOL-SPIKE): the same latent facts enter once as cluster evidence and again
        as exec multipliers, so single-component deltas UNDERSTATE redundancy;
        this arm exposes it
```
**Pre-registered demotion rule (a rule, not a report — reports get rationalized):**
a sizing-active component keeps sizing rights only if its ablation delta is
non-negative on OOS Sharpe or on tail metrics (Monte Carlo P(DD > 15%)); otherwise
it is demoted to log-only at the next annual WF cycle. Promotions (log-only → active,
multiplier → veto) follow the same schedule using the B5 counterfactual cut report.

---

### B7e — Risk-Scaling Study (pre-registered path to a higher risk cap)

The baseline caps (0.5%/trade, 0.8%/day) are sized for an unvalidated system whose own
EV-realisation gate tolerates 2× overconfidence (realised ≥ 50% of predicted). They are
correct for go-live. This step exists so a higher owner risk tolerance (target ~2%/day)
can be honored LATER, by measurement rather than decree, and as a pre-registered
procedure — never a mid-drawdown discretionary decision.

**The caps are a locked stack, not independent dials.** DAILY_LOSS_HALT is 1.2× the
daily cap by construction; MONTHLY_LOSS_HALT ≈ 5 max-loss days; the B7b Monte Carlo
DD gate was tuned at 0.8%/day. Drawdowns scale ~linearly with the risk multiplier, so
moving MAX_DAILY_RISK alone breaks the halt calibration and invalidates the MC
distribution. A raise moves the whole stack together:

| Constant | Baseline | ×2.5 (→ 2%/day) | Why it must move |
|---|---|---|---|
| MAX_RISK_PER_TRADE | 0.5% | 1.25% | keep the 0.625 ratio to the daily cap |
| MAX_DAILY_RISK | 0.8% | 2.0% | the target |
| DAILY_LOSS_HALT | 0.96% | 2.4% | stays 1.2× the daily cap by construction |
| MONTHLY_LOSS_HALT | 4% | ~10% | must stay ≈ 5 max-loss days or it trips on ordinary variance |
| MAX_STOCK_NOTIONAL | 100% of cash | ~300–400% of cash | 1.25% risk on a 0.5% stop = Rs 25L notional |
| MC DD gate | P(DD > 15%) < 5% | P(DD > ~30%) < 5%, owner-signed | DD distribution scales ~2.5× |
| KELLY_FRACTION | 0.10 | 0.10 (unchanged) | the caps, not the fraction, are the control surface |

**The study (run once, alongside B7b + the B5b stress suite):**
re-execute the B7b Monte Carlo and the full S1–S7 stress suite at risk-cap multipliers
×1.5, ×2.0, ×2.5 (per-trade and daily scaled together). The **safe raise ceiling** is
the highest multiplier at which EVERY B7b/S1–S7 gate still passes. Report the DD
distribution, P(negative year), and 5th-pct Sharpe at each multiplier so the cost of
each step is visible, not just pass/fail.

**S7 is the binding stress:** a −4.2R circuit trap (B5b's own worked example) at
1.25%/trade risk is −5.25% in a single fill — it breaches the scaled daily halt on one
trade. Any multiplier whose S7 run produces single-fill losses above the daily halt
fails the study regardless of aggregate Sharpe.

**Physical ceiling, independent of appetite:** at Rs 10L cash and SEBI's 20% MIS margin
floor, total notional ≤ ~50L. Risking 2% (Rs 20,000) on a p% stop needs Rs 20,000/p%
of notional — a 0.5% stop demands Rs 40L (80% of buying power on ONE position); below a
~0.4% stop, 2%/day is undeployable. The study reports, per multiplier, the fraction of
historically-taken trades whose stop was too tight to carry the scaled risk — i.e. how
much of the raise is real vs theoretical. The system's tightest-stop, highest-RR setups
(its best trades) are exactly the ones that cannot carry the higher risk; at 10L the
binding constraint is leverage, not tolerance.

**Preconditions for actually raising — all, in order, none skippable:**
1. All Phase 3 exit criteria pass at BASELINE caps (ECE, Brier, EV realisation, DSR,
   PBO, S1–S7).
2. 10–15 live paper sessions with fills matching backtest.
3. ≥ 3 months real-money live at baseline caps, slippage within model, zero unexplained
   PROB_DRIFT / CHANGEPOINT alarms.

**Then step, never jump:** 0.8 → 1.2 → 1.6 → 2.0%/day, one step per calendar month,
each step conditional on (a) the multiplier being at or below the study's safe ceiling
AND (b) live tracking still holding at the current step. Any step that triggers a
monthly halt or a drift alarm reverts to the prior step for the next month. The owner
signs the rewritten DD gate (≈ 30%, ≈ Rs 3L at 10L capital) in writing before step 1.

**Files:** `reports/risk_scaling.py` — wraps B7b/S1–S7 in the multiplier sweep and emits
the per-multiplier gate table. No new engine logic; a scaled re-run of existing machinery.

---

### B8 — Benchmark Testing + Capacity Analysis

**B8a — Benchmarks (`reports/benchmarks.py`):** the Bayesian system's OOS results are
meaningless without context. Run each benchmark through the same execution simulator,
costs, and 1L+1S/day constraint:

| Benchmark | Definition |
|---|---|
| Random entry | Random stock + direction at 09:30, same RR geometry, same sizing caps |
| Always-ORB | ORB-15 on every qualifying day, no gates |
| Always-VWAP | VWAP-REV on every qualifying day, no gates |
| Current production system | The existing weight-based system, as-is |
| Buy & hold Nifty | Passive reference for the same period |

Gate: Bayesian system beats random entry and the current production system on Sharpe
with statistical significance (bootstrap p < 0.05). If it can't beat random entry,
the gates are selecting noise.

**B8b — Capacity analysis:** re-run WF-8 (most recent frozen posteriors) at simulated
capital of Rs 10L / 50L / 2Cr / 10Cr. Position sizes scale with capital; the execution
simulator's participation-rate impact term and the 1%-of-ADV liquidity cap do the rest.
The Phase 1 MARGIN_CHECK must be active in these runs — at 2Cr+ with the same 0.5%
risk rule, tight stops routinely demand notional beyond 5× cash, and without the
check the capacity numbers are overstated.
Report Sharpe and slippage cost per capital level → documents the strategy's capacity
ceiling (where Sharpe degrades > 20% from the 10L baseline). Even a manual estimate
belongs in the final report.

---

### Phase 4 (post-validation — build only after Phase 3 gates pass)

**Phase 4a — Automated Order Execution (broker-agnostic; Groww primary)**

Trigger: all Phase 3 exit criteria pass AND the system has run 10–15 sessions in
live PAPER (shadow) mode — the full engine runs against live market data and
simulates its orders end-to-end (entry, stop, target, square-off) with no human in
the loop; paper fills, trade selection, and PnL must match backtest expectations.
Then the same engine is pointed at the real-money broker adapter.
Max 1 LONG + 1 SHORT per day is unchanged. There is no manual order placement in
any mode; the only human touchpoint is the B5 loss-threshold approval gate.

**Layering principle:** NSE only executes primitive order types (market, limit,
SL trigger). Bracket orders, OCO, GTT, MIS auto square-off, and blocked lists are all
BROKER-side software, not exchange features. The engine therefore assembles its
bracket from exchange-native primitives only — which every API broker supports —
behind a broker adapter interface:

```
BrokerAdapter (interface): place_order / modify / cancel / order_status /
                           positions / quotes_stream
Implementations: GrowwBroker (primary), ZerodhaBroker (later switch = swap, not rewrite)
```

Order state machine per position (identical on any broker):

```
1. ENTRY     MIS marketable-limit order at recommended price;
             fill confirmed via the broker's order-update stream (or polling)
2. PROTECT   immediately on fill: SL stop order placed AT THE BROKER —
             the trigger sits at the exchange, so protection survives
             local crash / network loss
3. TARGET    monitored in software from the live market-data feed;
             on touch: cancel SL, exit with marketable limit
             (OCO emulated in software — NEVER park stop and target sell
             orders simultaneously: two open sells against one MIS position
             over-sell and open an unintended short)
4. EOD       force square-off at 15:10, ahead of the broker's forced MIS
             square-off (~15:15–15:20, which carries a per-position charge)
5. RECONCILE every order event logged; local position state reconciled
             against the broker positions API every 5 minutes;
             mismatch → alert + halt new entries (protective exits keep running)
6. REJECT    broker RMS rejection (MIS block etc.) → [BROKER_REJECTED] log +
             skip the trade, no retry; recurring rejections on F&O names → alert
```

**Groww API verification checklist (complete during the 10–15 day live-paper
phase, before real money — use 1-share probe orders to exercise the real order
path where simulation can't answer):**
- SL-M vs SL-limit support for cash-equity MIS orders
- Order-update delivery: socket vs polling, and typical fill-confirmation latency
  (step 2 depends on fast fill detection)
- Cancel/modify latency and rate limits (≤ 6 orders/day makes limits unlikely
  to bind, but confirm)
- MIS auto square-off exact timing and charges
- If any item fails verification, switch the adapter to ZerodhaBroker — the
  state machine is unchanged

**Live tick-pipeline verification (same 10–15 day window):**
- Kite's tick `volume_traded` is CUMULATIVE for the day, not per-tick.
  `live/candle_builder.py` already handles this correctly (per-bar volume =
  cumulative-at-close − cumulative-at-open, with re-baseline on mid-day restart) —
  verify it in practice: for ≥ 3 sample days, diff the live-built 5-min bars
  against Kite's official historical candles for the same day (price AND volume);
  volume mismatch > 2% on any bar → investigate before automation
- Restart test: kill and resume the agent mid-session; confirm the resumed bar's
  volume is not inflated by the missing baseline
- If the market-data feed ever moves to Groww's API, re-verify its volume
  semantics first — cumulative vs per-trade volume differs across feeds, and
  every volume-driven signal (VOL-SPIKE, participation-rate slippage, ADV cap)
  silently corrupts if the assumption flips

Notes:
- Zerodha-specific: Kite GTT supports native two-leg OCO (stop + target) but is
  designed for CNC/delivery, not MIS intraday — not used even if switching brokers
- Safety: kill switch that halts new entries but leaves protective exits live;
  shares one mechanism with the B5 loss-threshold halt & approval gate
- Regulatory: SEBI's retail algo-trading framework (effective 2025) requires API
  orders to be tagged; above order-rate thresholds, algo registration via the broker
  with a static IP. This system sits in the lowest tier at 2 trades/day — confirm
  the broker's registration requirements before real-money go-live
- Groww has no published daily MIS blocked list (Zerodha does). The shorts-F&O-only
  universe rule (Phase 1 B2) was chosen because it is broker-portable and removes
  most of the blocked-short risk without any list; residual live rejections are
  handled by step 6

**Phase 4b — Meta-learner (out of scope until ≥ 6 months of live trades)**

An XGBoost/LightGBM layer that learns take-trade/skip-trade from Bayesian outputs,
regime, clusters, stock personality, volatility, and volume — not replacing the
strategies, just learning when to trust them. Requires a live track record to train
on without recycling the same backtest data a third time.

---

## 3. WHAT TO COMPARE AT THE END

| Metric | Current system | Bayesian system | Target |
|---|---|---|---|
| Monthly return (Rs) | baseline | no regression target | EV gate reduces trade count; absolute Rs PnL may be flat — gains are risk-adjusted |
| Sharpe Ratio | baseline | ≥ baseline + 0.3 | Better risk-adjusted returns |
| Max Drawdown | baseline | ≤ baseline | No regression |
| Trades per year | baseline | fewer | Quality over quantity — EV and cluster gates filter weak setups |
| Avg EV per trade entered | not measured | > 0.40 | Every entered trade has real edge |
| Calibration ECE | not measured | < 0.05 | Probability estimates are trustworthy |
| Brier Score | not measured | < 0.22 | System knows what it doesn't know |
| clusters_confirmed=1 trades | included | excluded | Correlated-only setups rejected |
| Weight update lag | 20 days | 0 — every trade | Structural fix |
| Outcome modeling | binary 0/1 | continuous PnL-normalised | Structural fix |
| Stock personality | ignored | stock_type_mu per symbol | HDFCBANK treated differently from trend stocks |
| Regime awareness | hard multipliers (unvalidated) | learned per-regime posteriors | Data-driven, not guessed |
| Deflated Sharpe Ratio | not measured | > 0 at 95% confidence | Edge survives multiple-testing correction |
| PBO (overfitting probability) | not measured | < 0.20 | Backtest selection is not luck |
| Monte Carlo P(DD > 15%) | not measured | < 5% | Drawdown risk quantified, not anecdotal |
| Stress scenarios S1–S7 | not run | Sharpe > 0 in all | Edge survives imperfect execution and circuit traps |
| vs Random entry / current system | not compared | beats both, p < 0.05 | Improvement has context |
| Entry/execution quality | not modeled | exec_mult (ms × ee × cq) + chase veto + counterfactual logs | Lower-quality executions filtered; every cut measurably earns its existence |

---

## 4. PHASE 3 EXIT CRITERIA (LIVE DEPLOYMENT GATES)

All must pass before the system is used for live paper trading:

| Check | Requirement |
|---|---|
| ECE | < 0.05 in at least 4 of the WF windows |
| Brier Score | < 0.22 in WF-5 (most recent full OOS year proxy) |
| EV realisation | Realised PnL/risk ≥ 50% of predicted EV in WF-5 |
| Sharpe Ratio | ≥ current system baseline + 0.3 across full OOS period |
| Max Drawdown | ≤ current system baseline across full OOS period |
| 2020 COVID test | System did not blow up; R4 regime activated; change-point alarms fired; drawdown contained |
| 2022 bear year test | Short-side EV gate passed meaningful trades; losses contained |
| Deflated Sharpe Ratio | > 0 at 95% confidence over full OOS period |
| PBO | < 0.20 via CSCV on 2019–2026 OOS trades |
| Monte Carlo | P(max DD > 15%) < 5%; P(negative year) < 10%; 5th-pct Sharpe > 0 |
| Stress tests S1–S7 | Sharpe > 0 and DD ≤ 1.5× unstressed in every scenario |
| Circuit-trap accounting | [CIRCUIT_TRAP] losses appear uncapped in Monte Carlo and drawdown stats |
| Slippage calibration | Per-tier constants set from the 1-min VWAP sample study, not defaults |
| Short-universe counter | % of gated shorts outside point-in-time F&O universe measured and documented |
| Benchmarks | Beats random entry AND current production system on Sharpe, bootstrap p < 0.05 |
| Capacity | Capacity ceiling documented; Rs 10L operation ≥ 2× below ceiling |
| Probability drift alarm | Fires on synthetic 15-pt drift within one month of trades; zero false alarms on calibrated stream |
| Calibration report | Auto-generates after each test year without manual trigger |
| Live posteriors | wf8_bayes.json is the file loaded at system startup for live paper trading |
| Risk-scaling study | B7e run: MC + S1–S7 swept at ×1.5/×2.0/×2.5; safe cap ceiling documented. Go-live uses BASELINE 0.5%/0.8% regardless — a raise requires the B7e preconditions (all Phase 3 exits + live paper + 3 months clean live) |
| Exec-layer ablation | B7d run with all-off, per-component, weighted-contradiction, and redundancy arms; every sizing-active component's delta documented; demotions applied per the pre-registered rule |
| Counterfactual coverage | 100% of exec-layer cuts and contradiction-gate-only rejections carry simulated outcomes through the full WF run |
| Exec sensitivity | ±25% perturbation of every B4e/B4f scalar constant: no full-period Sharpe sign flip |
| DSR accounting | Trials count demonstrably includes exec-layer constants and any log-only → active promotions |
| Exec-quality of entered trades | Average exec_mult of entered trades > 0.7 across the OOS period (the layer discounts marginal entries; it must not be degrading the typical one) |
| Loss decomposition | Monthly report generated from the excursion record; never-worked / gave-back / truncated classes populated per strategy × regime; capture ratio and winner-MFE-vs-target reported |
| Config integrity | settings_hash present on 100% of trades; a synthetic mid-window settings.py change raises `[CONFIG_DRIFT]`; `settings_manifest.md` exists and matches the sweep list |
