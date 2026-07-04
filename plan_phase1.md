# Phase 1 — Foundation Layer
**Build steps: B0 (Correlation Audit) → B1 (Bayesian State) → B2 (Scorer + Gates + Engine)**
**Phase 2 and Phase 3 cannot start until all Phase 1 exit criteria pass.**

---

## 0. WHY REPLACE THE EXISTING WEIGHT SYSTEM

Five statistical flaws in the current system, all fixed in this phase:

| # | Flaw | Current behaviour | Consequence |
|---|---|---|---|
| 1 | Fixed batch cycle | Weights recalculate every 20 days | Strategy weight is up to 19 days stale after a trade |
| 2 | Hard thresholds on small samples | >60% on last 20 signals → boost | SE of 20 binary outcomes = ±11.2%; noise triggers boost/suppress |
| 3 | No uncertainty quantification | 3/5 wins treated same as 60/100 wins | System equally confident in 4 data points and 100 data points |
| 4 | No probability-based entry gate | Any signal passing quality filters is traded | Trades with near-zero or negative EV are entered |
| 5 | Binary outcome modeling | Win=1, Loss=0 — magnitude ignored | Trade that hit 95% of target and one that hit 10% both score "1" |

**Signal output: before and after**
```
Current:
  SIGNAL [SHORT]: VMM | driver=BOLLINGER | entry=119.93 | target=116.59 stop=121.74 RR=1.85
  | agreeing=4 score=12.00 pred=64.4% | size=Rs 499,988 [MEDIUM]

After Phase 1:
  SIGNAL [SHORT]: VMM | driver=BOLLINGER | entry=119.93 | target=116.59 stop=121.74 RR=1.85
  | clusters=2(B,C) eff=1.7 driver_mu=0.644 EV=+0.83 | risk=Rs 5,000 (0.5%) size=Rs 3.3L [EV gate: PASS]
```

Entry, target, stop, driver strategy — all unchanged.
What changes: how confidence is estimated, what gates entry, how size is calculated.

---

## 1. WHAT STAYS THE SAME

| Parameter | Value |
|---|---|
| Capital | Rs 10,00,000 |
| Universe | Nifty 500 |
| Data | 5-min OHLCV, Kite Connect, 2016–2026 |
| Initial training window | 2016–2018 (3 years before first WF freeze) |
| Backtesting (OOS) starts | 2019 — not 2023 like the current system |
| Max positions/day | 1 LONG + 1 SHORT (2 trades/day when both qualify) |
| Transaction cost model | Unchanged (brokerage, STT, slippage) |
| Quality filters | Existing unchanged (liquidity, RR ≥ 1.5, time, volume, circuit) + macro event calendar filter (new) + universe eligibility filter (new — shorts F&O-only; see B2) |
| Broker | Groww primary; possible later switch to Zerodha (Kite). Execution layer is built broker-agnostic (Phase 4a adapter). Note: Zerodha publishes a daily MIS blocked list, Groww does not — the shorts-F&O-only universe rule (B2) was chosen precisely because it is broker-portable and does not depend on any published list. Live order rejections are handled gracefully: `[BROKER_REJECTED]` log + skip, no retry |
| Strategy library | 32 existing strategies only (16 new strategies added in Phase 2) |
| Walk-Forward | 5 windows, same manual trigger rhythm |
| Output | paper_trades.csv, daily trade log, year summaries |
| Execution | Fully automated — no manual order placement for entry or exit, ever. Rollout: backtest gates pass → 10–15 sessions in live PAPER (shadow) mode (engine runs on live data, orders simulated end-to-end) → real money via broker API (Phase 4 spec in plan_phase3.md). The ONLY human touchpoint: loss-threshold approval gate — if daily or monthly loss crosses its threshold the system halts itself and requires explicit approval to resume next session (Phase 3 B5). Max 1 LONG + 1 SHORT per day in all modes |

---

## 2. THE BAYESIAN CORE

### 2a. State Representation

**Current:** list of last 50 binary outcomes per strategy per direction.
**Bayesian:** Beta distribution (α, β) per strategy per direction, with decay.

```json
{
  "BOLLINGER": {
    "long":  { "alpha": 47.5, "beta": 31.5, "n_eff": 73.1 },
    "short": { "alpha": 28.0, "beta": 18.0, "n_eff": 40.1 }
  },
  "RSI-EXT": {
    "long":  { "alpha": 3.0, "beta": 3.0, "n_eff": 0.0 },
    "short": { "alpha": 3.0, "beta": 3.0, "n_eff": 0.0 }
  }
}
```

Strategies with no trade history start at exactly Beta(3,3) — prior only.
They contribute near-neutral weight until they accumulate real evidence.

### 2b. Prior

**Prior: Beta(α₀=3, β₀=3)** for every strategy, every direction, on day one.

Posterior mean at prior = 50%. After ~60 real outcomes the data dominates the prior.
- β(1,1): too flat — overconfident after 2 trades
- β(10,10): too strong — takes 30 trades to move estimate 5%
- β(3,3): balanced — stable estimates after one WF training window

### 2c. Update Rule — PnL-Normalised, Not Binary

Every trade updates the posterior immediately on settlement.
A trade that captured 90% of the target updates the posterior more than one that captured 10%.

```
normalised_pnl = actual_pnl_rs / risk_amount
                 (risk_amount = abs(entry − stop) × shares)

score = (normalised_pnl + 1) / (RR + 1)     maps [−1, +RR] → [0, 1]

Examples at RR = 1.85:
  Full target hit   :  score = (1.85+1)/(2.85) = 1.00
  90% of target     :  score = (1.665+1)/(2.85) = 0.93
  Break-even        :  score = 1/2.85           = 0.35
  10% of target     :  score = (0.185+1)/(2.85) = 0.42
  Full stop hit     :  score = 0/2.85           = 0.00
```

**Outlier handling — robust update (winsorization):**

Gap-through exits can produce normalised_pnl far outside [−1, +RR]: a circuit/news
event can hand a single trade +12R. Unclamped, one freak trade distorts the posterior
for months and inflates every EV computed from it.

```
normalised_pnl = clip(normalised_pnl, −1.5, RR)   # winsorize: max loss 1.5R, max gain = target
score          = clip(score, 0.0, 1.0)
```

Every clipped trade is logged `[OUTLIER_WINSORIZED raw=+12.3R used=+1.85R]` — the tail
event stays visible in the trade log and PnL accounting; only the posterior sees the
capped value. Real PnL is never winsorized, only the Bayesian evidence score.

**Update scope — driver-only (previously unstated, now explicit):**

Only the DRIVER strategy's posterior updates from a trade's outcome. Strategies that
fired alongside it (confirming or contradicting) do NOT update — they did not set the
trade's geometry, and crediting a dozen co-firing posteriors with one outcome is
pseudo-replication: one trade would masquerade as twelve pieces of evidence.

Consequence, stated so it cannot surprise anyone later: clusters that are never
driver-eligible (E — Context, F — Meta) accumulate no trade evidence and sit at
(or near) their prior indefinitely. The confirmation gate therefore treats their
votes at fixed weight (Section 2g), and their actual predictive value is measured
by the Phase 3 strategy importance report and consumed by the Phase 4b meta-learner
— not by a posterior that can never learn.

**Exit policy is part of the frozen spec (previously implicit, now binding):**

Exits are inherited unchanged from the current system: target touch, stop hit, 15:10
EOD square-off — no trailing stops, no partial exits, no discretionary closes. Because
the evidence score above is computed from exit outcomes, every posterior learns
strategy quality and exit policy JOINTLY. Consequence: any change to exit logic
(trailing stop, different square-off time, partial profit-taking) silently invalidates
all accumulated evidence — the posteriors would describe a system that no longer
exists. Exit-policy changes are therefore spec changes, not settings tweaks: they
require a full B6 walk-forward re-run under the new policy before live use, exactly
like a change to the B4e level menu. Improving exits is a legitimate future project —
but it is a project with its own WF cycle, never a tweak.

The same joint learning applies to execution reality: a circuit trap or slippage
overrun updates the driver's posterior as strategy evidence (winsorized at −1.5R) —
prevention lives in the universe and liquidity filters, and the winsorization bounds
the distortion. The per-trade excursion record (B2) is collected from day one
precisely so the future exit project starts with a decade of evidence instead of zero.

### 2d. Alpha Decay — Edges Change Over Time

Without decay, a 2016 trade has equal weight to a 2026 trade.
Market patterns evolve — what worked in 2018 may be crowded by 2024.

**Applied on every update:**
```
DECAY = 0.999   (configurable in settings.py)

alpha *= DECAY
beta  *= DECAY
alpha += score
beta  += (1 − score)
```

Effect of DECAY=0.999 on evidence age:
- After 250 days (1 year)  : retains 77.8% weight
- After 750 days (3 years) : retains 47.2% weight
- After 1750 days (7 years): retains 17.2% weight

This is NOT a rolling window. All history is retained but recent outcomes matter more.
The posterior never resets. Old evidence fades, it does not disappear.

### 2e. Bayesian Weight Formula

Posterior mean (best estimate of true win rate):
```
mu = alpha / (alpha + beta)
```

Conservative lower bound — 25th-percentile credible interval:
```
mu_conservative = scipy.stats.beta.ppf(0.25, alpha, beta)
```

Weight used in composite score:
```
weight = max(MIN_WEIGHT, min(MAX_WEIGHT, (mu_conservative − 0.40) × WEIGHT_SCALE))
```

WEIGHT_SCALE set so: mu_conservative=0.65 → weight=3.0, mu_conservative=0.50 → weight=1.0,
mu_conservative=0.40 → weight=MIN_WEIGHT(0.1).

Thin evidence: 25th percentile far below mean → weight stays low automatically.
Dense evidence: 25th percentile converges to mean → weight earns its level.

### 2f. Bayesian Expected Value — Primary Entry Gate

**Most important use of the posterior. Determines whether to enter at all.**

**Step 1 — Model-uncertainty shrinkage (applied before EV is computed):**

The posterior mean is itself an estimate. With thin evidence, a measured mu = 0.63 may
truly be 0.55 — and Kelly sizing on the optimistic number compounds the error. Shrink
toward the neutral prior in proportion to evidence:

```
w      = n_eff / (n_eff + SHRINK_K)        SHRINK_K = 30 (settings.py)
P(win) = w × mu + (1 − w) × 0.50

n_eff = 10  → w = 0.25 → P barely moves off 0.50 → EV gate rarely passes
n_eff = 60  → w = 0.67 → posterior mostly trusted
n_eff = 200 → w = 0.87 → shrinkage nearly gone
```

All EV computation, gating, and sizing below use the shrunk P(win) — never raw mu.
(The Beta(3,3) prior already shrinks a little; this adds explicit protection against
model uncertainty, not just sampling noise.)

**Step 2 — EV:**
```
Bayesian EV = P(win) × RR − (1 − P(win))
            = P(win) × (RR + 1) − 1
Break-even P = 1 / (1 + RR)
```

**Entry decision (soft gate — see Section 2g for the ramp):**
```
EV < 0.15         →  skip — no edge, regardless of clusters or agreement
0.15 ≤ EV < 0.25  →  enter at linearly reduced size (ramp)
EV ≥ 0.25         →  enter at full computed size
```

| P(win) | RR=1.85 | EV | Decision |
|---|---|---|---|
| 35% | 1.85 | −0.00 | Skip |
| 45% | 1.85 | +0.28 | Marginal pass |
| 64.4% | 1.85 | +0.83 | Enter |
| 75% | 1.85 | +1.14 | Enter, large size |

EV replaces composite score as ranking criterion.
Two candidates same day: take higher EV, not higher score.

### 2g. Setup Confirmation — Independent Cluster Gate

`agreeing=4` raw count is wrong. Four correlated strategies all firing on a breakout
(ORB-15, ORB-30, VOL-SPIKE, SR-BREAK) is one piece of evidence counted four times.

**Strategy clusters — Phase 1 uses 32 existing strategies across clusters A–E:**

```
Cluster A — Breakout (7):
  ORB-15, ORB-30, PDH-PDL, GAP-CONT, VOL-SPIKE, SR-BREAK, FAILED-BO

Cluster B — Mean Reversion (5):
  VWAP-REV, RSI-EXT, BOLLINGER, GAP-FADE, STOCHASTIC

Cluster C — Trend Following (3):
  EMA-CROSS, SUPERTREND, MACD

Cluster D — Price Structure & Candlestick (11):
  NR7, FIRST-CANDLE, PIN-BAR, INTRADAY-STRUCT, BEAR-ENGULF,
  DBL-TOP, DESC-TRI, RISE-WEDGE, BEAR-FLAG, DEAD-CAT, OPEN-WEAK

Cluster E — Context & Overlay (6):
  CPR, CAMARILLA, ADX-FILTER, VWAP-SD, VOL-PROFILE, REL-STR

[Clusters F and G are empty in Phase 1 — added in Phase 2 with new strategies]
```

**Every strategy fires with a direction — LONG or SHORT, never both at once.**
Strategies with two firing conditions (e.g. EMA-CROSS) have two independent posteriors:
```json
"EMA-CROSS": {
  "long":  { "alpha": 35.0, "beta": 25.0, "n_eff": 54.1 },
  "short": { "alpha": 28.0, "beta": 22.0, "n_eff": 44.1 }
}
```

**Confirmation gate (replaces `agreeing >= N`):**
```
clusters_confirmed     = distinct clusters with ≥1 strategy firing IN driver direction
clusters_contradicting = distinct clusters with ≥1 strategy firing AGAINST driver direction

require: eff_weighted >= 1.5  AND  eff_binary >= 1.5  AND  clusters_contradicting <= 1
(raw clusters_confirmed >= 2 is a necessary precondition; eff_binary and eff_weighted
are the same quadratic form evaluated on two vote vectors — defined below)
```

A non-firing cluster is NEUTRAL — absence of confirmation is not contradiction.
A cluster counts as contradicting only when a strategy in it actively fires the opposite direction.

**Dependency penalty — effective independent evidence:**

Distinct clusters are far less correlated than same-cluster strategies, but they are
NOT independent. One underlying market move — a breakout — expands volume, breaks the
candlestick, flips the trend indicator, and lifts momentum. Breakout, Trend, and
Structure clusters all fire from the same latent event. Counting them as separate
votes overstates the evidence.

B0 estimates the inter-cluster signal correlation matrix **C** (one row/column per
cluster, from co-firing frequency in training data). For a setup where the confirming
clusters have indicator vector v:

```
eff(v) = (Σ v)² / (vᵀ C v)      # equals raw count when C = I and votes are binary;
                                # shrinks toward 1 as clusters co-fire
eff_binary   = eff(v), v ∈ {0,1} per confirming cluster
eff_weighted = eff(c), c = confidence weights (Vote weighting, below)

Example — clusters A and C confirm, corr(A,C) = 0.6:
  raw count = 2 → effective = 4 / (2 + 2×0.6) = 1.25 → REJECTED (< 1.5)

Example — clusters B and D confirm, corr(B,D) = 0.15:
  raw count = 2 → effective = 4 / (2 + 0.3)   = 1.74 → PASSES
```

Gate: `eff_weighted >= 1.5 AND eff_binary >= 1.5`. C is estimated in B0 from
training-period signals only and re-estimated at every WF freeze — never from
test-period data.

**Vote weighting — a cluster's vote is only as strong as its evidence (v1: confirmation side only):**

A cluster confirming through a strategy at P(win)=0.51 and one confirming through
P(win)=0.65 are not equal evidence, yet binary votes count them identically. Confirming
votes therefore carry confidence weights:

```
c_i = max(C_FLOOR, min(1, (P_best,i − 0.50) / 0.15))      C_FLOOR = 0.3 (settings.py)

P_best,i = shrunk P(win) (§2f) of the strongest strategy firing in cluster i in the
           driver direction. Shrinkage tempers the winner's-curse bias of taking a
           max over many members (13 strategies in cluster D); B0 sanity-checks the
           resulting c distribution.

Clusters E and F: c_i = 1.0 FIXED. Driver-only updates (§2c) leave their posteriors
at prior, so a learned weight would floor them forever — structurally silencing
exactly the votes this plan calls "real opposition worth counting" (a PCR reading
against the driver). They vote binary until signal-level outcome tracking exists.
```

eff() is scale-invariant in its vote vector — multiply every vote by k and the k²
cancels — so uniform confidences reproduce binary behaviour exactly. Early-run, when
every price cluster sits at the 0.3 floor, eff_weighted = eff_binary and the gate is
effectively unweighted. Weights only start mattering once posteriors diverge.

**Why the gate is dual (weighted AND binary):** the quadratic form is not monotone in
c. Example: corr(1,2) = 0.9 plus an independent third cluster — binary votes give
eff = 9/4.8 = 1.875; LOWERING c₁ to 0.9 gives 8.41/4.43 = 1.899. Downweighting one of
a correlated pair reduces double-counting and RAISES eff. Mathematically sound,
perverse as decision behaviour: a confirming posterior degrading must never flip a
reject into a pass. Requiring eff_binary ≥ 1.5 as well makes weighting a pure
tightening — weights can take size or approval away, never rescue a failed setup.

**Contradiction votes stay raw in v1 — deliberately.** A weighted contradiction rule
(Σ c_j ≤ 1.0 with a raw ≤ 2 backstop) would ADMIT trades the binary rule rejects: two
floor-confidence oppositions sum to 0.6 and pass where `contradicting ≤ 1` rejected
them. That is a loosening disguised as a reweighting, and bundling it with the
confirmation-side tightening would make the Phase 3 ablation unattributable. v1 keeps
`clusters_contradicting <= 1` unchanged. Every setup rejected ONLY by the
contradiction gate that would pass the weighted rule is counterfactually simulated
through the walk-forward and logged `[CF_CONTRA realized_R=...]`; the weighted rule is
enabled at an annual WF cycle only if that logged set shows positive realized R.
Pre-registered, not discretionary.

Example — driver = BOLLINGER SHORT (Cluster B):
```
RSI-EXT   SHORT (B) → same cluster as driver; driver already counted
PIN-BAR   SHORT (D) → confirms  → clusters_confirmed = 2
CPR at R  SHORT (E) → confirms  → clusters_confirmed = 3
EMA-CROSS LONG  (C) → CONTRADICTS → clusters_contradicting = 1

Result: confirmed=3, contradicting=1 → PASSES → [CONFIRMED — CONTESTED]
```

```
confirmed=2, contradicting=2 → REJECTED — equal opposition
confirmed=1, contradicting=0 → REJECTED — single cluster
confirmed=2, contradicting=0 → PASSES   — clean confirmation
```

**Driver confidence gate (soft):**
```
driver_mu < 0.52          → reject
0.52 ≤ driver_mu < 0.58   → accept at linearly reduced size (ramp)
driver_mu ≥ 0.58          → full size multiplier
```

**Soft gates — no cliff at the threshold:**

A hard cutoff means a signal at EV=0.199 is rejected while EV=0.201 gets full size,
despite being statistically identical. The two scalar gates (EV, driver_mu) are
therefore sizing ramps, not binary cuts:

```
ev_mult     = clip((EV − 0.15) / 0.10, 0, 1)          # 0 at EV=0.15, 1 at EV=0.25
driver_mult = clip((driver_mu − 0.52) / 0.06, 0, 1)   # 0 at mu=0.52, 1 at mu=0.58

gate_mult   = ev_mult × driver_mult                    # multiplied into position size (2i)
gate_mult = 0 → trade rejected
```

Borderline setups are taken at token size — the posterior still collects evidence from
them — while capital concentrates in clearly positive-EV setups. Ramp anchor values are
not hand-tuned mid-run: they are frozen per WF window and sensitivity-tested in Phase 3.

**Full entry confirmation — all four must pass:**
```
1. eff_weighted >= 1.5 AND eff_binary >= 1.5   (confidence-weighted, dependency-penalised)
2. clusters_contradicting <= 1                 (raw count; weighted rule logged as [CF_CONTRA] only)
3. driver_mu >= 0.52              (ramped to full weight at 0.58)
4. EV >= 0.15                     (ramped to full size at 0.25; shrunk P(win) used)
```

**Additional rule — Breakout driver trend alignment:**
When the driver strategy is from Cluster A (Breakout), Cluster C (Trend) must NOT be
actively contradicting. The contradicting tolerance of ≤ 1 is reduced to 0 for Cluster C
specifically when a breakout is the driver.

Rationale: reversion drivers (Cluster B) are designed to work counter-trend — an opposing
EMA-CROSS is expected and one contradiction is acceptable. Breakout drivers are not
counter-trend by nature; a breakout against an active trend signal is a genuinely weaker
setup and should be rejected.

```
Driver from Cluster A + Cluster C firing AGAINST direction → REJECTED (regardless of other gates)
Driver from Cluster A + Cluster C silent (no crossover)    → normal gate applies
Driver from Cluster A + Cluster C firing WITH direction    → normal gate applies (bonus confirmation)
Driver from Cluster B/C/D/G + Cluster C firing AGAINST    → normal tolerance (contradicting ≤ 1)
```

Signal log:
```
clusters=2(A,D) vs=1(C) driver=ORB-15 → [REJECTED — breakout driver blocked by trend opposition]
clusters=2(A,D) vs=0    driver=ORB-15 → [CONFIRMED — CLEAN]
clusters=2(B,D) vs=1(C) driver=BOLLINGER → [CONFIRMED — CONTESTED]  ← reversion, normal tolerance
```

Signal log:
```
clusters=3(B,D,E) vs=1(C) driver_mu=0.644 EV=+0.83 → [CONFIRMED — CONTESTED]
clusters=3(B,D,E) vs=0    driver_mu=0.644 EV=+0.83 → [CONFIRMED — CLEAN]
clusters=2(B,D)   vs=2    driver_mu=0.644 EV=+0.83 → [REJECTED — equal opposition]
clusters=1(B)     vs=0    driver_mu=0.644 EV=+0.83 → [REJECTED — single cluster]
clusters=2(B,D)   vs=0    driver_mu=0.51  EV=+0.21 → [REJECTED — weak driver]
```

### 2h. Bayesian Composite Score (informational only)

Logged but not used as a gate:
```
score = Σ [ max(0, mu_strategy − 0.50) × 1.0 ]
```
Regime modifier is a flat 1.0 in Phase 1. Applied in Phase 2.
Strategies with mu < 50% contribute zero.

### 2i. Position Sizing — Capped Fractional Kelly + Portfolio Risk Limits

Replace 3 hard tiers (HIGH / MEDIUM / STANDARD) with fractional Kelly — but Half-Kelly
is far too aggressive. At EV=0.8/RR=1.8, Half-Kelly risks 22% of capital on one trade.
No institutional fund risks anything close to that, because parameter, model, execution,
and regime uncertainty all stack on top of the estimate. Kelly fraction is cut to 0.10
and hard-capped by portfolio constraints:

```
KELLY_FRACTION  = 0.10                                  (settings.py; never above 0.15)

Kelly           = EV / RR                               (EV uses shrunk P(win), Section 2f)
posterior_scale = 1 − (CI_width / CI_width_at_prior)    (0=uncertain, 1=confident)
gate_mult       = ev_mult × driver_mult                 (soft-gate ramps, Section 2g)

risk_fraction   = KELLY_FRACTION × Kelly × posterior_scale × gate_mult
risk_amount     = capital × min(risk_fraction, MAX_RISK_PER_TRADE)
position_size   = min(MAX_STOCK_NOTIONAL, risk_amount / stop_pct)   # notional cap defined below
```

**The chain grows in Phase 2 — this stays the single canonical definition:** B4e
appends `exec_mult` (execution quality) and B4f appends `context_mult` (breadth rule),
giving the full live chain
`KELLY_FRACTION × Kelly × posterior_scale × gate_mult × exec_mult × context_mult`,
still capped by MAX_RISK_PER_TRADE and the portfolio limits below. Both new terms are
1.0 until their build steps ship. No other multiplier enters sizing anywhere in the
system.

**Portfolio-level constraints (hard caps, checked after per-trade sizing):**

Risk-based caps are the primary control; notional caps are secondary sanity bounds.
(Notional caps are denominated against intraday buying power — cash × MIS leverage —
not raw cash. The earlier 5%-of-capital stock cap was an institutional multi-position
convention that contradicts this system's own examples: a 2-position intraday book
naturally runs 20–50% of cash as notional per position.)

```
MAX_RISK_PER_TRADE   = 0.5%  of capital     (risk_amount cap)
MAX_DAILY_RISK       = 0.8%  of capital     (sum of risk across the day's trades;
                                             second trade is shrunk to fit, or skipped)
MAX_STOCK_NOTIONAL   = 100% of cash capital per position (= 20% of 5× buying power)
SECTOR_RULE          = when LONG and SHORT are both open, they must be in
                       different NSE sectors
LIQUIDITY_CAP        = position notional ≤ 1% of the stock's 20-day average daily
                       turnover (Nifty 500 tail names are illiquid; this cap is what
                       keeps the fill model in B2 honest)

MARGIN_CHECK (before every trade):
  required_margin = Σ over open positions: notional × margin_rate(stock)
                    margin_rate = max(VaR + ELM, 20%)   — SEBI peak-margin floor
  require: required_margin ≤ available cash
  If breached: size the trade down to fit; skip if it can't; log [MARGIN_CAPPED]

  Note the failure direction: TIGHT stops blow up notional (0.5% risk with a 0.1%
  stop demands 5× cash in notional), not wide ones. Rare at Rs 10L; binds routinely
  in the Phase 3 capacity runs at 2Cr+ — without this check, capacity is overstated.
```

**These caps are a locked stack, not independent dials (raise path: Phase 3 B7e).**
MAX_RISK_PER_TRADE, MAX_DAILY_RISK, both loss-halts (Phase 3 B5), MAX_STOCK_NOTIONAL,
and the Monte Carlo DD gate are mutually pinned: the daily halt is 1.2× the daily cap
by construction, the monthly halt ≈ 5 max-loss days, and the MC gates were tuned at
0.8%/day. Changing MAX_DAILY_RISK alone breaks the halt calibration and the validated
DD distribution. A higher owner risk tolerance (up to ~2%/day) is honored only through
the pre-registered, post-validation, stepped raise in Phase 3 B7e, which moves the whole
stack together and re-runs the MC + stress gates at the new level. Go-live uses baseline
0.5%/0.8% regardless. (Note: at Rs 10L cash, SEBI's 20% margin floor caps total notional
near 50L, so below a ~0.4% stop a 2%/day cap is physically undeployable — the honest way
to deploy more risk-capital is more capital, not a larger cap.)

**Integer-share rounding (checked last, after all caps):**

The soft gates deliberately produce token-size trades (gate_mult ≈ 0.1 → risk Rs 500 →
notional ~Rs 50k). On high-priced stocks (MRF ~Rs 1.3L/share; Page, Bosch, Shree
Cement similar) the optimal share count can be fractional or zero, and rounding to a
whole share can multiply the intended risk several times over.

```
shares      = floor(position_notional / price)
actual_risk = shares × |entry − stop|

if shares == 0 or |actual_risk − intended_risk| / intended_risk > 0.25:
    skip the trade — log [LOT_ROUND_SKIP]

Posterior updates and all reporting use actual_risk, never theoretical risk.
Realized-vs-intended risk is logged on every trade.
```

Example — BOLLINGER short, EV=0.83, RR=1.85, posterior_scale=0.8, gate_mult=1.0:
- Kelly = 0.83/1.85 = 44.9%
- 0.10 × Kelly × 0.8 = 3.6% → capped at MAX_RISK_PER_TRADE = 0.5%
- risk = Rs 5,000 on Rs 10L capital → position sized to this risk

The per-trade cap will bind on most high-EV trades. That is intentional: Kelly output
ranks conviction and scales *down* for weak setups; the cap owns the ceiling.

New strategies (posterior_scale ≈ 0): tiny position until evidence accumulates.
Proven strategies: scales up continuously to the cap — no tier jumps.

---

## 3. KEY DESIGN DECISIONS

| Decision | Choice | Why |
|---|---|---|
| Prior | Beta(3,3) | Stable estimates after ~60 trades; not overconfident early |
| Outcome scoring | PnL-normalised [0,1], winsorized at [−1.5R, +RR] | Magnitude matters; one +12R freak trade must not distort the posterior |
| Decay | 0.999 per trading day | Recent evidence dominates without discarding all history |
| Weight | 25th-percentile credible bound | Penalises uncertainty; scales up only as evidence accumulates |
| Model uncertainty | Shrink P(win) toward 0.50 by n_eff/(n_eff+30) | Posterior mean is itself an estimate; sizing on the optimistic number compounds error |
| Entry gate | EV soft ramp 0.15→0.25 | Refuses trades with no statistical edge; no cliff at the cutoff |
| Confirmation | eff_weighted ≥ 1.5 AND eff_binary ≥ 1.5 + contradicting ≤ 1 + driver_mu ramp 0.52→0.58 + EV ramp | Four independent gates replace raw agreeing count |
| Vote weighting | c ∈ [0.3, 1] from shrunk P(win) of strongest firing strategy; E/F fixed at 1.0; dual gate | A 51% cluster and a 65% cluster are not equal evidence; the quadratic form is non-monotone, so weighting must only ever tighten |
| Posterior update scope | Driver-only | Crediting co-firing strategies recycles one outcome into a dozen posteriors (pseudo-replication) |
| Cluster independence | effective_clusters via inter-cluster correlation matrix | Distinct clusters still co-fire off one latent move; raw cluster count overstates evidence |
| Direction per strategy | LONG or SHORT explicitly per fire | EMA-CROSS bearish and bullish tracked with independent posteriors |
| Position sizing | 0.10 Kelly × posterior_scale × gate_mult, capped at 0.5%/trade | Half-Kelly risks ~22% per trade — untenable under parameter/model/regime uncertainty |
| Portfolio limits | 0.8% daily risk, 100%-of-cash stock notional, margin check, sector separation, 1%-of-ADV cap | Per-trade sizing alone is not risk management |
| Execution realism | Fill at next-bar open + slippage model | Close-of-signal-candle fills are look-ahead; backtest edge must survive realistic fills |
| Correlation audit first | Before any code | Correlated pairs inflate composite score; assignments must be data-driven |

---

## 4. BUILD STEPS

### B0 — Correlation Audit (prerequisite, before any code)

1. Load existing trade logs from the current system — **restricted to data ≤ 2018**
   (the WF-1 training boundary). Correlations and cluster assignments are frozen
   structural choices; computing them over 2019–2025 data would import future
   co-behavior into the walk-forward — lookahead by structure rather than by value.
2. Compute pairwise signal correlation matrix for all 32 existing strategies.
3. Flag pairs with Pearson r > 0.70 → assign weight cap max_weight = 1.5.

   Expected high-correlation pairs:
   - ORB-15 vs ORB-30
   - BOLLINGER vs KELTNER-REV (KELTNER-REV added in Phase 2; note here for reference)
   - EMA-CROSS vs SUPERTREND
   - RSI-EXT vs STOCHASTIC

4. Assign all 32 existing strategies to clusters A–E. Place per correlation groupings
   from step 2. Save to `config/strategy_clusters.json` — used by confirmation gate.
5. **Estimate the inter-cluster correlation matrix C** (cluster-level co-firing
   correlation, one row/column per cluster) from training-period signals. Save to
   `config/cluster_corr.json` — used by the effective_clusters dependency penalty
   (Section 2g). Re-estimated at every WF freeze from training data only.
6. Count regime-labelled trades per strategy → verify n_eff_regime ≥ 20 is achievable
   for at least 3 strategies per regime (confirms Phase 2 regime split is worth building).
7. Vote-weighting sanity: compute the c distribution and the eff_weighted vs eff_binary
   pass rates over training-period signals. Confirm the dual gate's pass set is a strict
   subset of the binary pass set (weighting only tightens) and the tightening removes
   < 15% of binary-passing setups — a sanity check on C_FLOOR and the 0.15 scale,
   not a tuning loop.

**Gate: review audit output before writing any code.**

---

### B1 — Bayesian State Layer

**Replaces:** `weights/adaptive.py`, `winrate_updater.py`

**Files:**
- `weights/bayesian.py` — `BayesianState` class
  - Stores alpha, beta, n_eff per strategy × direction
    (regime dimension is added in Phase 2)
  - `update(strategy, direction, pnl_rs, risk_amount, rr)` — PnL-normalised score + decay
  - `get_posterior(strategy, direction)` — returns mu, mu_conservative, CI_width, Kelly_fraction
- `checkpoints/strategy_bayes.json` — written after every trade, all 32 strategies

**Validation:** Seed from 2016–2017 existing trade logs. Verify:
- Well-known strong strategies (ORB-15 long, BOLLINGER short) show mu > 0.60
- Known weak strategies (DESC-TRI as driver) show mu < 0.50
- Strategies with no trade history sit at exactly mu = 0.50

**Seeded state is discarded before the walk-forward run.** Seeding exists only to
validate the update machinery against known strategy behaviour. Old-system trades
were generated under different gates, sizing, and exits — as live evidence they would
contaminate the posterior with outcomes the new system would never have produced.
The B6 run (Phase 3) starts every posterior clean at Beta(3,3) in 2016, and the live
posteriors (wf8) descend exclusively from that clean run.

---

### B2 — Bayesian Scorer + Cluster Gate + EV Gate + Engine Wiring

**Replaces:** `backtester/composite_scorer.py` (partial), `backtester/engine.py` (entry logic)

**Files:**
- `backtester/composite_scorer.py`
  - `bayesian_long_score()` / `bayesian_short_score()` using `max(0, mu − 0.50) × 1.0`
    (flat 1.0 multiplier in Phase 1; regime modifier wired in Phase 2)
  - `count_clusters(signals, driver_direction, cluster_map)` → returns
    `clusters_confirmed`, `clusters_contradicting`, the confidence vector `c`,
    and `eff_binary` / `eff_weighted` (dependency penalty via
    `config/cluster_corr.json`; vote weights per Section 2g)
  - Each strategy fires with explicit direction tag; firing opposite to driver = contradiction
- `backtester/engine.py` — after quality filters:
  1. `eff_weighted >= 1.5 AND eff_binary >= 1.5` gate (raw clusters_confirmed >= 2 precondition)
  2. `clusters_contradicting <= 1` gate — plus `[CF_CONTRA]` counterfactual log for
     every setup this gate alone rejects that the weighted rule would admit
  3. `driver_mu >= 0.52` soft gate (ramp to 0.58)
  4. `EV >= 0.15` soft gate (ramp to 0.25; shrunk P(win))
  5. Rank by execution-discounted EV = EV × exec_mult × context_mult; take the highest
     LONG + highest SHORT. Both multipliers are 1.0 until Phase 2 B4e/B4f ship, so
     Phase 1 ranking is plain EV — pinned now because ranking and sizing must agree:
     a setup forced to quarter size by execution quality is worth less than a
     slightly-lower-EV setup taken at full size
  6. Position size via capped fractional Kelly + portfolio limits (Section 2i)
- Signal log format:
  `clusters=X(B,D) c=(0.8,0.4) eff_w=1.55 eff_bin=1.7 vs=Y(C)  driver_mu=Z  EV=+W  gate_mult=0.84  [CONFIRMED—CLEAN / CONFIRMED—CONTESTED / REJECTED—reason]`

**Baseline execution realism (part of B2 — full simulator arrives in Phase 3 B5b):**

The current backtest fills at the close of the signal candle. That is look-ahead —
the close is only known once the bar completes. From B2 onward every backtest fill is:

```
entry_price = next bar OPEN
            + slippage(spread_estimate, order_size / bar_volume)

slippage default = 5 bps + impact term proportional to participation rate
                   (both configurable in settings.py; deliberately conservative)
```

Exits (target/stop) fill at the touched level ± the same slippage model; if a bar gaps
through the stop, fill at the bar open, not the stop price. All EV realisation and
calibration metrics in Phase 3 are computed against these degraded fills.

**Per-trade excursion record (part of B2 — every trade, winners included):**

```
paper_trades.csv gains: MFE_R, MAE_R   (max favorable / adverse excursion in R,
                                        measured from 5-min bar extremes — the
                                        intra-bar path is unknowable; same
                                        convention in backtest and live paper)
                        bars_to_exit
                        exit_reason ∈ {TARGET, STOP, EOD, CIRCUIT_TRAP}
                        settings_hash  (config integrity stamp — Phase 3 B5)
```

Purpose — retrospective credit assignment. A loss decomposes into: **never worked**
(straight to stop → entry thesis wrong — the when/what lens), **gave it back**
(MFE > 1R before the stop → exit-policy candidate — the when-to-sell lens), or
**truncated** (EOD exit with positive MFE → time budget). Winners matter equally:
median winner MFE vs target measures whether the frozen targets leave money on the
table. This record is the design basis for the future exit project named in §2c —
log-only, no decision reads it; the monthly decomposition report lives in Phase 3 B5.

**Validation:** Run 2016–2018 training years with new gates.
Expected: fewer trades than current system, higher average EV per trade entered.
If trade count drops > 60%, widen the EV ramp start to 0.10 (do not remove the ramp).

---

### Universe Eligibility Filter (additional quality filter, part of B2)

Market liquidity and broker shorting eligibility are different things on the NSE.
Non-F&O stocks carry hard daily circuit limits (5/10/20%); a cash-equity intraday
short trapped at an upper circuit cannot square off → short delivery → exchange
auction penalty. To protect against that, broker RMS desks (Zerodha, Groww) block
MIS shorting on a daily, discretionary list covering roughly the bottom 200–300 of
the Nifty 500 — a list that is NOT predictable from volume or ADV filters. Left
unmodeled, the backtest assumes short fills the live market will reject, and the
walk-forward Sharpe diverges from live PnL.

F&O-segment stocks have dynamic price bands (no hard lock), so brokers essentially
never block MIS shorts on them outside extreme tail events.

**Rule (applied in `backtester/engine.py` alongside the other quality filters):**
```
SHORT candidates: restricted to the point-in-time F&O-eligible cash-equity universe
LONG  candidates: full Nifty 500 MINUS stocks in T2T/BE series, GSM stage ≥ 2,
                  or ASM long-term stages (compulsory-delivery series cannot be
                  traded intraday on the long side either)

Live (additional): cross-check every candidate against the broker's daily MIS
blocked list before the recommendation is shown — [BROKER_BLOCKED] log + skip.
```

**Point-in-time F&O membership — survivorship warning:**
The F&O list is highly non-stationary (~180 names through most of the backtest
window; 45 additions in Nov 2024 alone; quarterly reviews). Backtesting shorts
against *today's* list is lookahead AND survivorship bias — today's F&O members
were yesterday's momentum winners, a biased short universe.

- `config/fno_membership.json` — reconstructed from NSE circulars, keyed by date;
  the engine queries membership as-of the signal date
- Broker MIS lists are not archived historically, so point-in-time F&O membership
  is the honest backtestable proxy; accept that live eligibility will run slightly
  tighter than backtest (measured by the B6 counter, Phase 3)

**Point-in-time Nifty 500 membership — the universe itself (both sides):**
The same survivorship logic applies to the plan's largest universe decision, and was
previously unhandled: index membership churns ~10%/year, so running 2016 signals over
TODAY'S constituent list backtests only the survivors and winners.

- `config/nifty500_membership.json` — date-keyed, reconstructed from NSE/NIFTY Indices
  rebalance archives (same mechanism as `fno_membership.json`); the engine queries
  membership as-of the signal date
- Applies to every universe-level computation: candidate scans on both sides, the
  1%-of-ADV liquidity cap, and the Phase 2 B4f breadth tag — breadth computed over
  today's members instead of as-of members would silently inherit the bias

**Point-in-time regulatory flags and sector map (same artifact class, previously
unlisted):**
- `config/asm_gsm_history.json` — date-keyed ASM/GSM stage and T2T/BE series flags,
  reconstructed from NSE's archived surveillance circulars; the LONG-side exclusion
  rule above must evaluate these as-of the signal date, not against today's list
- `config/sector_map.json` — date-keyed NSE sector classification (stocks get
  reclassified over a 10-year window); consumed by the SECTOR_RULE (§2i) and the
  Phase 2 B4f sector_rs tag

---

### Data Integrity — Corporate Action Adjustment (part of B2)

Nothing in the plan previously guaranteed split/bonus-adjusted data, yet PDH-PDL,
PWH-PWL, GAP-CONT and GAP-FADE are raw price-level strategies running over 2016–2026
— a period with hundreds of Nifty 500 splits and bonuses. A 1:5 split reads as an
−80% overnight gap: GAP-FADE fires on a phantom signal, PDH is meaningless, and the
posterior absorbs a fake outcome. Kite adjusts daily candles but locally stored
intraday Parquet is frozen at whatever adjustment state it was downloaded in.

**Rules:**
1. Build a corporate-action calendar (splits, bonuses, rights) from NSE
   Bhavcopy/CA archives → `config/corporate_actions.json`
2. Back-adjust the 5-min Parquet store with cumulative adjustment factors;
   re-adjust on every new CA
3. **Audit gate:** flag every overnight gap > 25% in the full dataset; each must
   match a CA record or a news-verifiable event. Phase 1 exit criterion: zero
   unexplained > 25% gaps.
4. Runtime: skip any stock on its ex-date (wired into the Phase 2 earnings-skip
   calendar infrastructure)

---

### Macro Event Calendar Filter (additional quality filter, part of B2)

On RBI MPC days, Union Budget day, and US Fed decision days, all intraday patterns
break — VWAP levels lose meaning, breakouts fail more, mean reversion overshoots.
The Bayesian posteriors will learn this eventually but need years of evidence per event
type (only 6 RBI days per year). A hard calendar filter is immediate and costs nothing.

**Rule:**
```
MACRO_EVENTS = ["RBI_MPC", "UNION_BUDGET", "US_FED_DECISION"]

if today in events_calendar[MACRO_EVENTS]:
    mode = settings.EVENT_DAY_MODE   # "SKIP" or "RAISE_THRESHOLD"

    if mode == "SKIP":
        skip all trades for the day — log reason: EVENT_DAY_SKIP
    if mode == "RAISE_THRESHOLD":
        MIN_EV_THRESHOLD = 0.35      # vs normal 0.20
        clusters_confirmed_required = 3   # vs normal 2
```

Default: `SKIP`. Set `RAISE_THRESHOLD` only if you want to keep trading on event days
with a deliberately higher bar.

**Implementation:**
- `config/events_calendar.json` — list of known event dates, updated annually
  (RBI publishes the full year MPC schedule in advance; Fed publishes FOMC schedule;
  Budget date is announced ~2 weeks prior)
- Checked in `backtester/engine.py` before any signal evaluation, as the very first gate
- Signal log: `[EVENT_DAY_SKIP: RBI_MPC]` — one log line, no further processing

**Populated for backtesting:** scrape/manually enter historical RBI MPC dates 2016–2026,
Union Budget dates, and US FOMC dates. These are all publicly available.

**What this does NOT cover:** unscheduled/breaking news (geopolitical, regulatory).
The R4 regime (CRASH) catches the aftermath of those events automatically.

---

### First-Candle Block (additional time filter, part of B2)

The 09:15–09:20 bar is the most manipulated candle of the day — widest spreads, lowest
liquidity, institutions absorbing overnight order imbalance. Many strategies fire on this
bar because their indicators are pre-computed from yesterday's close, but the signal is
noise: the gap is still sorting itself out and most crossovers reverse within 2–3 bars.

**Two categories of strategies:**

```
FIRST_CANDLE_EXEMPT:
  GAP-CONT, GAP-FADE      ← designed to fire on the gap; blocking defeats the purpose
  FIRST-CANDLE            ← explicitly evaluates the 09:15 bar by definition
  PDH-PDL, PWH-PWL        ← pre-computed price levels; touching them at open is the signal
  CPR, CAMARILLA          ← same — fixed daily levels computed overnight
  (Phase 2: PCR, DAY-SEASONALITY, PRE-EXPIRY, BLOCK-DEAL — meta signals, not price-bar dependent)

FIRST_CANDLE_BLOCKED (all other strategies):
  EMA-CROSS, SUPERTREND, MACD  ← crossover on bar 1 is gap noise, not a real trend signal
  BOLLINGER, RSI-EXT, STOCHASTIC, VWAP-REV  ← oscillators/bands need a few bars to settle
  ORB-15, ORB-30          ← structurally cannot fire before 09:30 / 09:45 anyway
  SR-BREAK, VOL-SPIKE     ← volume and range signals need intraday context to form
  All candlestick patterns ← PIN-BAR, BEAR-ENGULF, etc. need a completed bar in context
```

**Rule:**
```python
# backtester/engine.py — applied after macro event check, before signal evaluation

FIRST_CANDLE_EXEMPT = {
    "GAP-CONT", "GAP-FADE", "FIRST-CANDLE",
    "PDH-PDL", "PWH-PWL", "CPR", "CAMARILLA"
}

if current_bar_time == "09:15":
    signals = [s for s in signals if s.strategy in FIRST_CANDLE_EXEMPT]
    # all other strategy signals on this bar are silently dropped
```

**Effect:** signals from EMA-CROSS, BOLLINGER, SR-BREAK etc. firing at 09:15 are
discarded before reaching the cluster gate. They can fire from 09:20 onward.
Strategies in FIRST_CANDLE_EXEMPT fire normally at 09:15.

**Signal log:** no log entry for dropped first-candle signals — they are pre-filtered,
not rejected. Rejection log is reserved for signals that passed the time filter but
failed the cluster/EV/driver gates.

---

### Last-Entry Cutoff (additional time filter, part of B2)

The inherited "time" quality filter is now pinned explicitly. A signal at 09:30 and a
signal at 14:40 carry identical target/stop geometry, but the 14:40 trade has ~30
minutes before the 15:10 square-off truncates it — its EV is structurally overstated
because the geometry assumes time the trade does not have.

```
LAST_ENTRY_TIME = 14:30   (settings.py; frozen per WF window)

No NEW entries at or after LAST_ENTRY_TIME. Exits are unaffected — stops, targets,
and the 15:10 square-off run to the close as always.
```

Late-window performance is measured, not assumed: the Phase 2 B4f time-bucket tag (T3)
and the Phase 3 B5 per-bucket report quantify how trades entered 13:00–14:30 actually
realise their EV; if T3 realisation sits materially below T1/T2, tightening the cutoff
is an annual-cycle change like any other constant.

---

## 5. PHASE 1 EXIT CRITERIA

All must pass before starting Phase 2:

| Check | Requirement |
|---|---|
| B0 audit complete | `strategy_clusters.json` written; correlated pairs flagged |
| B0 cluster correlation | `cluster_corr.json` written; effective_clusters reproducible from it |
| B1 seeding | ORB-15 long mu > 0.60 after seeding 2016–2017 logs |
| B1 seeding | Strategies with no trade history sit at mu = 0.50 exactly |
| B1 shrinkage | P(win) for a strategy with n_eff=10 sits within 0.03 of 0.50 regardless of raw mu |
| B1 winsorization | A synthetic +12R trade updates the posterior identically to a +1.85R trade; `[OUTLIER_WINSORIZED]` logged |
| B2 gate logging | All four rejection reasons appear in logs (opposition / low-effective-clusters / weak-driver / low-EV) |
| B2 soft gates | Entered trades near EV=0.16 show gate_mult < 0.2 in the log (ramp working) |
| B2 sizing caps | No trade risks > 0.5% of capital; no day risks > 0.8%; violations impossible by construction, verified in logs |
| B2 execution | All fills at next-bar open + slippage; zero fills at signal-candle close |
| B2 trade count | 2016–2018 trade count lower than current system |
| B2 EV per trade | Average EV of entered trades > 0.40 |
| B2 signal log | CLEAN and CONTESTED variants both appear |
| B2 universe filter | Zero SHORT trades outside point-in-time F&O membership; `fno_membership.json` date-keyed and sourced from NSE circulars |
| B2 data integrity | Zero unexplained overnight gaps > 25% after CA back-adjustment; audit report written |
| B2 rounding | No trade's realized risk deviates > 25% from intended; `[LOT_ROUND_SKIP]` appears for high-priced stocks at token size |
| B2 margin check | `[MARGIN_CAPPED]` fires on synthetic tight-stop test (0.1% stop at 0.5% risk); no trade exceeds 5× cash notional |
| B2 vote weighting | c per confirming cluster, eff_weighted, and eff_binary all present in signal log; all-at-floor synthetic case gives eff_weighted = eff_binary exactly |
| B2 dual gate | Synthetic correlated-pair case (corr 0.9 + independent third cluster): downweighting one vote raises eff_weighted — trade still rejected when eff_binary < 1.5 |
| B2 contradiction counterfactual | `[CF_CONTRA]` logged with simulated outcome for every setup rejected only by the contradiction gate that the weighted rule would admit |
| B2 driver-only update | A settled trade changes exactly one strategy × direction posterior; E/F posteriors provably untouched after a full training year |
| B2 PIT universe | Zero trades on stocks outside as-of Nifty 500 membership; `nifty500_membership.json` date-keyed from NSE rebalance archives |
| B2 last entry | Zero entries at/after LAST_ENTRY_TIME in training-year logs; exits still run to the 15:10 square-off |
| B2 excursion record | MFE_R, MAE_R, bars_to_exit, exit_reason, settings_hash present on 100% of trades — winners included |
| B2 PIT flags/sectors | ASM/GSM/T2T exclusions and SECTOR_RULE evaluated from date-keyed files, not today's lists |
