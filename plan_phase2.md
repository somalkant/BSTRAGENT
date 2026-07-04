# Phase 2 — Enrichment Layer
**Build steps: B3 (Regime Posteriors) → B3b (Stock Behavior Prior) → B3c (Hierarchical Cluster Priors) → B3d (Change-Point Detection) → B4 (16 New Strategies) → B4e (Execution Quality Layer) → B4f (Context Layer v1 + Trade Tags)**
**Requires Phase 1 exit criteria to be fully passed before starting.**

---

## CONTEXT: WHAT PHASE 1 DELIVERED

After Phase 1, the system has:
- Beta posteriors per strategy × direction (32 strategies), driver-only updates
- Four-gate entry filter: eff_weighted ≥ 1.5 AND eff_binary ≥ 1.5 (confidence-weighted,
  dependency-penalised clusters), contradicting ≤ 1, driver_mu soft ramp 0.52→0.58,
  EV soft ramp 0.15→0.25 on shrunk P(win)
- 0.10-Kelly position sizing with hard caps (0.5%/trade, 0.8%/day, 100%-of-cash
  notional, margin check, 1%-of-ADV liquidity cap)
- Execution realism: next-bar-open + slippage fills; point-in-time universes
  (Nifty 500, F&O, ASM/GSM, sectors); per-trade excursion record (MFE/MAE/exit_reason)
- Regime field exists in the engine but is always "global" — one flat posterior per strategy

Phase 2 adds seven layers on top:
1. **Regime-conditional posteriors** — separate win rates per market regime, with hysteresis and boundary blending so regime detection itself doesn't have hard-threshold problems
2. **Per-stock behavior prior** — HDFCBANK is a mean-reverter; momentum smallcaps trend. Same strategy, different weight modifier per stock
3. **Hierarchical cluster priors** — strategies in the same cluster share statistical strength; a 35-trade strategy borrows evidence from its family instead of standing alone
4. **Bayesian change-point detection** — detects when a strategy's edge has structurally changed, instead of waiting months for exponential decay to catch up
5. **16 new strategies** — fills genuine signal gaps across 7 clusters; 48 strategies total
6. **Execution quality layer (B4e, from the third external review)** — the Phase 1 gates grade the SETUP; nothing grades the ENTRY. Market-structure acceptance, entry efficiency/anti-chasing, and trigger-candle quality become multiplicative size modifiers; momentum quality is computed but log-only
7. **Context layer v1 + trade tags (B4f)** — day-type, universe breadth, sector strength, daily-trend, and time-bucket tags on every trade (the Phase 4b feature set), plus exactly one sizing rule (breadth opposition)

---

## 1. REGIME MODEL

### Current state after Phase 1

`classify_regime()` exists but returns a flat "global" label. All trades use the global
posterior regardless of market conditions.

### Target: 4 regime-conditional posteriors per strategy per direction

**VIX threshold is a rolling percentile, not a fixed level.** India VIX's baseline
shifts structurally across years — 2017 and 2023 spent months at 10–13 where a fixed
VIX > 20 never fires (starving R1 of data for 18+ months), while 2020–2021 sat above
20 for long stretches (starving R3). Worse than starvation is mislabeling: VIX 19 in
2017 was locally extreme volatility, but a fixed threshold files those trades under
R3 and contaminates that posterior. A rolling percentile is deterministic and
lookahead-free (computed from data ≤ t−1 only), so it does not violate WF freeze
discipline — it is a rule, not a fitted parameter.

```
VIX_R1_LEVEL(t) = 80th percentile of India VIX closes over trailing 252 trading days
                  (computed from data up to t−1 only)

Regime states:
  R1: HIGH_VIX    (VIX > VIX_R1_LEVEL(t))
  R2: TRENDING    (VIX ≤ VIX_R1_LEVEL(t), ADX > 25)
  R3: SIDEWAYS    (VIX ≤ VIX_R1_LEVEL(t), ADX ≤ 25)
  R4: CRASH       (Nifty day-return < −2%)   ← stays ABSOLUTE — a crash is an
                                               absolute phenomenon, not a relative one

strategy_bayes.json per strategy, per direction:
  "global": { alpha, beta, n_eff }      always maintained
  "R1":     { alpha, beta, n_eff }      used only when n_eff_R1 ≥ 20
  "R2":     { alpha, beta, n_eff }
  "R3":     { alpha, beta, n_eff }
  "R4":     { alpha, beta, n_eff }
```

Fallback: global posterior when regime-specific n_eff < 20.
Decay applies independently per regime — regime edges also decay over time.
No manual multipliers. The data learns regime-conditional win rates.

### Fix 1 — Hysteresis Buffer (prevents regime thrashing at boundaries)

Hard thresholds (VIX > 20) can flip the regime every day when VIX oscillates around
the cutoff. Real regimes persist for weeks. A hysteresis buffer prevents this.

**Rule: a regime change only commits after the new condition holds for 3 consecutive trading days.**

```python
# weights/regime.py  —  classify_regime() with hysteresis

HYSTERESIS_DAYS = 3          # configurable in settings.py
_pending_regime = None
_pending_count  = 0
_active_regime  = "R3"       # default at startup

def classify_regime(vix, adx, nifty_pct, date):
    candidate = _raw_regime(vix, adx, nifty_pct)   # pure threshold logic

    if candidate == _active_regime:
        _pending_regime = None
        _pending_count  = 0
        return _active_regime

    if candidate == _pending_regime:
        _pending_count += 1
    else:
        _pending_regime = candidate
        _pending_count  = 1

    if _pending_count >= HYSTERESIS_DAYS:
        _active_regime  = candidate
        _pending_regime = None
        _pending_count  = 0

    return _active_regime    # still the old regime until buffer fills
```

R4 (CRASH) is exempt — a single day with Nifty < −2% triggers R4 immediately.
Buffer applies to R1/R2/R3 transitions only.

**R4 intraday, with positions open (previously unstated, now a decision):** Nifty can
cross −2% mid-session. From the bar the threshold is crossed, R4 is active — all NEW
entries are blocked immediately (no hysteresis, same as the daily rule). Open positions
are NOT force-flattened: the broker-held stop remains the exit, and the Phase 3 B5 loss
halt owns the account-level response. Rationale: panic-flattening into a crash
systematically sells the low; stop discipline plus the halt already bound the loss, and
both are validated in the 2020 COVID replay. Stated here so it is never re-litigated
mid-crash.

Signal log when pending:
```
regime=R3[pending→R1 day2/3]   ← regime not yet committed
regime=R1                       ← committed after 3 consecutive days
```

### Fix 2 — Boundary Blending (no discontinuity at threshold edge)

Blend the two adjacent regime posteriors linearly by distance from the boundary.
A trade just below the threshold and one just above should not receive dramatically
different strategy weights.

**The VIX blend band is defined in percentile space, not absolute points.** With a
rolling percentile threshold, the threshold *value* moves daily — a fixed ±1.0 point
band is sometimes 3 percentile points wide, sometimes 15. The band is therefore the
zone between the trailing 75th and 85th percentile values:

```python
# Applied in BayesianState.get_posterior_blended()

VIX_BAND_LO   = trailing 75th percentile value (252d, data ≤ t−1)   # blend zone floor
VIX_BAND_HI   = trailing 85th percentile value (252d, data ≤ t−1)   # blend zone ceiling
                # threshold itself = trailing 80th percentile value
ADX_THRESHOLD = 25.0
ADX_BAND      = 2.0     # blend zone: ADX ∈ [23.0, 27.0] — ADX stays absolute

def get_posterior_blended(strategy, direction, vix, adx, active_regime):
    # VIX boundary: R1 / R3 or R1 / R2
    if VIX_BAND_LO < vix < VIX_BAND_HI and active_regime in ("R1", "R2", "R3"):
        w_r1    = (vix - VIX_BAND_LO) / (VIX_BAND_HI - VIX_BAND_LO)
        w_r1    = max(0.0, min(1.0, w_r1))
        w_other = 1.0 - w_r1
        other   = "R2" if adx > ADX_THRESHOLD else "R3"
        p_r1    = get_posterior(strategy, direction, "R1")
        p_other = get_posterior(strategy, direction, other)
        mu_blended = w_r1 * p_r1.mu + w_other * p_other.mu
        ci_blended = w_r1 * p_r1.ci_width + w_other * p_other.ci_width
        return blended(mu_blended, ci_blended)

    # ADX boundary: R2 / R3 (VIX already ≤ 20)
    if abs(adx - ADX_THRESHOLD) < ADX_BAND and active_regime in ("R2", "R3"):
        w_r2   = (adx - (ADX_THRESHOLD - ADX_BAND)) / (2 * ADX_BAND)
        w_r2   = max(0.0, min(1.0, w_r2))
        p_r2   = get_posterior(strategy, direction, "R2")
        p_r3   = get_posterior(strategy, direction, "R3")
        mu_blended = w_r2 * p_r2.mu + (1 - w_r2) * p_r3.mu
        ci_blended = w_r2 * p_r2.ci_width + (1 - w_r2) * p_r3.ci_width
        return blended(mu_blended, ci_blended)

    # Outside any blend zone: use committed regime posterior as-is
    return get_posterior(strategy, direction, active_regime)
```

R4 (CRASH) is never blended. Blend output is used only for EV computation and scoring.

Signal log:
```
regime=R3  vix=19.3  [blend: 35% R1 / 65% R3]
regime=R2  adx=25.8  [blend: 40% R2 / 60% R3]
regime=R1  vix=22.1                              ← outside band, no blend
```

---

## 2. PER-STOCK BEHAVIOR PRIOR

The current system applies identical strategy weights to HDFCBANK and a momentum smallcap.
This is wrong. Some stocks consistently mean-revert (HDFCBANK, INFY); others trend strongly
after breakouts (momentum smallcaps, sector leaders).

### State: one Beta posterior per stock

```json
{
  "HDFCBANK": { "trend_alpha": 6.0,  "trend_beta": 14.0, "n_eff": 14.1 },
  "RELIANCE":  { "trend_alpha": 12.0, "trend_beta": 8.0,  "n_eff": 14.1 },
  "NEWSTOCK":  { "trend_alpha": 5.0,  "trend_beta": 5.0,  "n_eff": 0.0  }
}
```

Prior: Beta(5,5) → neutral 50/50 (more conservative than strategy prior).
Fallback: neutral modifier (stock_type_mu = 0.50) when n_eff_stock < 15.

### Update rule — applied every settled trade on this stock

```
If driver cluster is A (Breakout) or C (Trend):
  trend_alpha += score          ← trend strategy won → trend evidence
  trend_beta  += (1 − score)   ← trend strategy lost → reversion evidence

If driver cluster is B (Reversion):
  trend_beta  += score          ← reversion strategy won → reversion evidence
  trend_alpha += (1 − score)   ← reversion strategy lost → trend evidence

Clusters D, E, F, G: no update — structure/context/meta don't define stock personality
```

### Applied at scoring time as a cluster weight modifier

```
stock_type_mu = trend_alpha / (trend_alpha + trend_beta)

Cluster A (Breakout) and C (Trend):  mu_strategy × stock_type_mu × 2
Cluster B (Reversion):                mu_strategy × (1 − stock_type_mu) × 2
Clusters D, E, F, G:                  mu_strategy × 1.0  (neutral)
```

The × 2 factor keeps the maximum weight equivalent to no-modifier when stock_type_mu = 0.5.
Only the composite score (informational) is modified. EV gate and driver_mu thresholds
are NOT modified — entry gates are absolute, not stock-adjusted.

Signal log addition:
```
stock_type=trend(0.72)   → breakout/trend cluster scores boosted for this stock
stock_type=neutral(0.49) → n_eff < 15, flat 50/50 modifier
stock_type=revert(0.28)  → reversion cluster scores boosted for this stock
```

**Decision-inert in v1 — deliberate, stated the way mq is stated.** The stock-type
prior modifies only the composite score, which is informational (Phase 1 §2h) — no
gate, no EV, no ranking, and no sizing path reads it. It is a log-first learner: ten
years of stock-personality evidence accumulate through the walk-forward and become a
Phase 4b meta-learner feature with full history. Promoting it to a decision input
(modifying cluster votes or EV) is an annual-cycle change, counted in the DSR trials
like every other promotion.

---

## 3. HIERARCHICAL CLUSTER PRIORS — STRATEGIES SHARE STRENGTH WITHIN A FAMILY

### The problem

After Phase 1, each strategy learns in isolation. RSI-EXT with 35 trades and STOCHASTIC
with 40 trades are both mean-reversion oscillators reacting to the same market behaviour,
yet neither benefits from the other's evidence. For low-sample strategies (all 16 new
ones, and any strategy in a thin regime bucket), the isolated posterior is noisy exactly
when it matters most.

### The model — empirical-Bayes shrinkage toward the cluster

Maintain a pooled cluster-level posterior per cluster × direction (× regime in the
regime-split path): the decayed sum of all member strategies' evidence.

```
mu_cluster = pooled alpha / (pooled alpha + pooled beta)

w_hier  = n_eff_strategy / (n_eff_strategy + K_HIER)      K_HIER = 25 (settings.py)
mu_used = w_hier × mu_strategy + (1 − w_hier) × mu_cluster
```

- New strategy (n_eff ≈ 0): inherits the cluster's track record instead of a flat 0.50 —
  a new reversion strategy in a cluster that wins 58% starts near 0.58, not 0.50
- Established strategy (n_eff ≥ 100): w_hier ≈ 0.8+, its own evidence dominates
- A strategy genuinely different from its family earns its own level as evidence accumulates

Order of operations: hierarchical shrinkage first (strategy ← cluster), then the Phase 1
model-uncertainty shrinkage (mu_used → P(win) toward 0.50). Both are logged:
```
driver=RSI-DIV mu_raw=0.61 mu_hier=0.585(w=0.44 cluster_B=0.57) P_win=0.556
```

The stock-type prior (Section 2) is unchanged — it operates on the composite score,
not on P(win).

---

## 4. BAYESIAN CHANGE-POINT DETECTION — EDGE DEATH IS DETECTED, NOT DECAYED AWAY

### The problem

Decay 0.999 is correct for slow drift but blind to structural breaks. If a strategy's
edge died yesterday (crowding, microstructure change, F&O lot-size revision), decay
takes months to pull the posterior down. During those months the system keeps sizing
into a dead edge.

### The mechanism — Bayesian Online Change Point Detection (BOCPD)

Run BOCPD per strategy × direction on the stream of trade scores (the same [0,1]
evidence scores that update the posterior). BOCPD maintains a run-length posterior:
the probability that the data-generating process changed at each recent point.

```
On each settled trade:
  p_change = BOCPD run-length collapse probability (hazard prior: 1/200 trades)

  if p_change > 0.70 (settings.py: CHANGEPOINT_ALARM):
      1. Log prominently: [CHANGEPOINT strategy=ORB-15 dir=long p=0.83]
      2. Temper the posterior: alpha, beta ← prior + (alpha − prior_α) × 0.5,
                                             prior + (beta − prior_β) × 0.5
         (evidence halved toward Beta(3,3) — uncertainty re-inflates, CI widens,
          posterior_scale and therefore position size drop immediately)
      3. Do NOT zero the posterior — the alarm can be false; halving is recoverable
```

Effect: a real edge-death shows up in position size within ~10 trades instead of
~6 months. A false alarm costs a temporary size reduction, then evidence rebuilds.

This immediate size response is the **defensive-overlay exception** to the
walk-forward freeze rule (Phase 3 §1a): tempering acts on decisions within the current
test year AND live — reduction only, never an increase. Without the exception, this
entire mechanism would be inert until the next annual freeze.

Fallback implementation note: if full BOCPD proves heavy, a CUSUM detector on the score
stream with the same tempering action is an acceptable v1 — the tempering response is
the important part, the detector is swappable.

---

## 5. STRATEGY LIBRARY — 48 STRATEGIES ACROSS 7 CLUSTERS

Phase 1 runs with 32 existing strategies. Phase 2 adds 16 new ones.

### Why 48, not 32 and not 60

**Not 32:** The current 32 have genuine gaps. RSI Divergence, Fair Value Gap, Prior Week
levels, TTM Squeeze, and Fibonacci levels are well-established, widely-traded signals
completely absent from the current library. These are not variations of what exists —
they capture genuinely different market conditions.

**Not 60:** Beyond ~48, new additions are increasingly correlated with existing strategies.
Adding a 4th breakout strategy to Cluster A does not improve `clusters_confirmed` — it
stays at 1 regardless. More correlated signals = louder echo, not stronger evidence.

**48 is the right number** because it covers all 7 independent information dimensions
accessible from 5-min OHLCV + NSE free data, with no major gaps remaining.

### Full 48-strategy list

| # | Strategy | Cluster | Phase | Direction | Driver-eligible |
|---|---|---|---|---|---|
| 1 | ORB-15 | A — Breakout | 1 | Both | Yes |
| 2 | ORB-30 | A — Breakout | 1 | Both | Yes |
| 3 | PDH-PDL | A — Breakout | 1 | Both | Yes |
| 4 | GAP-CONT | A — Breakout | 1 | Both | Yes |
| 5 | VOL-SPIKE | A — Breakout | 1 | Both | Yes |
| 6 | SR-BREAK | A — Breakout | 1 | Both | Yes |
| 7 | FAILED-BO | A — Breakout | 1 | Both | Yes |
| 8 | **PWH-PWL** | A — Breakout | **2** | Both | Yes |
| 9 | **TTM-SQUEEZE** | A — Breakout | **2** | Both | Yes |
| 10 | VWAP-REV | B — Reversion | 1 | Both | Yes |
| 11 | RSI-EXT | B — Reversion | 1 | Both | Yes |
| 12 | BOLLINGER | B — Reversion | 1 | Both | Yes |
| 13 | GAP-FADE | B — Reversion | 1 | Both | Yes |
| 14 | STOCHASTIC | B — Reversion | 1 | Both | Yes |
| 15 | **KELTNER-REV** | B — Reversion | **2** | Both | Yes |
| 16 | **MFI-DIV** | B — Reversion | **2** | Both | Yes |
| 17 | **RSI-DIV** | B — Reversion | **2** | Both | Yes |
| 18 | **VOL-PRICE-DIV** | B — Reversion | **2** | Both | Yes |
| 19 | EMA-CROSS | C — Trend | 1 | Both | Yes |
| 20 | SUPERTREND | C — Trend | 1 | Both | Yes |
| 21 | MACD | C — Trend | 1 | Both | Yes |
| 22 | **PARABOLIC-SAR** | C — Trend | **2** | Both | Yes |
| 23 | NR7 | D — Structure | 1 | Both | Yes |
| 24 | FIRST-CANDLE | D — Structure | 1 | Both | Yes |
| 25 | PIN-BAR | D — Structure | 1 | Both | Yes |
| 26 | INTRADAY-STRUCT | D — Structure | 1 | Both | Yes |
| 27 | BEAR-ENGULF | D — Structure | 1 | Short | Yes |
| 28 | DBL-TOP | D — Structure | 1 | Short | Yes |
| 29 | DESC-TRI | D — Structure | 1 | Short | Yes |
| 30 | RISE-WEDGE | D — Structure | 1 | Short | Yes |
| 31 | BEAR-FLAG | D — Structure | 1 | Short | Yes |
| 32 | DEAD-CAT | D — Structure | 1 | Short | Yes |
| 33 | OPEN-WEAK | D — Structure | 1 | Short | Yes |
| 34 | **THREE-BAR-REV** | D — Structure | **2** | Both | Yes |
| 35 | **FVG** | D — Structure | **2** | Both | Yes |
| 36 | CPR | E — Context | 1 | Both | No |
| 37 | CAMARILLA | E — Context | 1 | Both | No |
| 38 | ADX-FILTER | E — Context | 1 | Both | No |
| 39 | VWAP-SD | E — Context | 1 | Both | No |
| 40 | VOL-PROFILE | E — Context | 1 | Both | No |
| 41 | REL-STR | E — Context | 1 | Both | No |
| 42 | **FIB-RETRACEMENT** | E — Context | **2** | Both | No |
| 43 | **PCR** | F — Meta | **2** | Both | No |
| 44 | **DAY-SEASONALITY** | F — Meta | **2** | Both | No |
| 45 | **PRE-EXPIRY** | F — Meta | **2** | Both | No |
| 46 | **BLOCK-DEAL** | F — Meta | **2** | Both | No |
| 47 | **MTF-15M-ORB** | G — Multi-TF | **2** | Both | Yes |
| 48 | **MTF-15M-EMA** | G — Multi-TF | **2** | Both | Yes |

Cluster E and F strategies are not driver-eligible — advisory vote only.

**Important — Cluster F and G counting rule:**
Cluster F (Meta) and Cluster G (Multi-TF) count toward BOTH `clusters_confirmed` AND
`clusters_contradicting` when they fire. A PCR reading strongly bullish while the driver
is SHORT is a real contradiction worth counting — not just noise.

```
PCR bullish, driver SHORT  → clusters_contradicting += 1   (real opposition)
PCR bearish, driver SHORT  → clusters_confirmed += 1       (meta confirms direction)
MTF-15M-EMA bearish, driver SHORT → clusters_confirmed += 1
```

Cluster E (Context) strategies follow the same rule — they are not driver-eligible but
their directional fire does count toward confirmed or contradicting totals.

### What the 16 new strategies add

| Strategy | What it captures | Why not already covered |
|---|---|---|
| PWH-PWL | Prior week high/low breakout | PDH-PDL covers only prior day; weekly levels carry more weight |
| TTM-SQUEEZE | Bollinger inside Keltner = coiled spring | Bollinger alone doesn't detect the squeeze condition |
| KELTNER-REV | ATR-based channel reversion | Bollinger uses std-dev; Keltner uses ATR — fires differently in trending markets |
| MFI-DIV | Volume-weighted RSI extreme/divergence | RSI-EXT uses price only; MFI incorporates volume |
| RSI-DIV | Price vs RSI divergence | RSI-EXT fires at extremes; divergence fires at weakening momentum before reversal |
| VOL-PRICE-DIV | Price new high + volume declining | VOL-SPIKE fires on high volume; this fires on declining volume = distribution signal |
| PARABOLIC-SAR | Trailing stop flip = trend reversal | EMA-CROSS and SUPERTREND track trend; SAR identifies the reversal point specifically |
| THREE-BAR-REV | Morning Star, Evening Star, 3WS, 3BC | PIN-BAR is single-candle; 3-bar patterns require confirmed reversal over 3 candles |
| FVG | 3-candle institutional imbalance zone | Nothing in the 32 captures order imbalances / unfilled gaps at institutional levels |
| FIB-RETRACEMENT | 38.2/50/61.8% pullback levels | CPR/Camarilla cover fixed pivot levels; Fib covers dynamic swing-based levels |
| PCR | Options Put-Call Ratio sentiment | Entirely new data type — options market sentiment |
| DAY-SEASONALITY | Day-of-week + expiry week bias | Temporal pattern, orthogonal to all price signals |
| PRE-EXPIRY | F&O expiry week directional tendency | Calendar effect with documented NSE-specific bias |
| BLOCK-DEAL | Institutional buy/sell before open | Institutional order flow — entirely new data type |
| MTF-15M-ORB | ORB confirmed on 15-min chart | 15-min is independent from 5-min — different timeframe = different information |
| MTF-15M-EMA | EMA cross confirmed on 15-min chart | Same independence argument — cross-timeframe confirmation |

### Correlation weight caps (from Phase 1 B0 audit)

Pairs expected to be highly correlated (r > 0.70) — one member gets max_weight capped at 1.5:
- ORB-15 vs ORB-30
- BOLLINGER vs KELTNER-REV
- EMA-CROSS vs SUPERTREND
- RSI-EXT vs STOCHASTIC
- RSI-DIV vs VOL-PRICE-DIV

Exact cap assignments confirmed from B0 audit results, not assumed.

---

## 6. BUILD STEPS

### B3 — Regime-Conditional Posteriors + Hysteresis + Boundary Blending

**Files:**
- Extended `strategy_bayes.json` schema — R1/R2/R3/R4 sub-entries per strategy × direction
- `BayesianState.update()` records regime tag with every trade
- `BayesianState.get_posterior()` uses regime-specific posterior when n_eff ≥ 20
- `weights/regime.py` — `classify_regime(vix, adx, nifty_pct)` with hysteresis buffer
  (Section 1, Fix 1): R1/R2/R3 transitions require 3 consecutive days; R4 immediate
- Rolling percentile threshold: `VIX_R1_LEVEL(t)` = trailing-252d 80th percentile,
  computed from data ≤ t−1 (Section 1); percentile parameters in `settings.py`
- `BayesianState.get_posterior_blended()` — boundary blending (Section 1, Fix 2):
  VIX blend zone = trailing P75→P85 values; ADX ±2 zone stays absolute;
  R4 never blended
- `HYSTERESIS_DAYS`, VIX percentile params, `ADX_BAND` configurable in `settings.py`
- All callers use `get_posterior_blended()`, not `get_posterior()` directly

**Validation:**
- After 2016–2018 training: compare R1 vs R2 posterior means for ORB-15. If difference
  < 2%, regime split adds no value — document and fall back to global for that strategy.
- Find VIX-oscillation periods in 2017–2018 data (VIX hovering near its trailing P80).
  Confirm hysteresis prevents same-day regime flips.
- Verify blend output is monotone as VIX sweeps the P75→P85 zone (no discontinuity).
- **Regime balance check:** R1 receives ≥ 15% of trading days in EVERY calendar year
  2016–2025 (this is the whole point of the percentile threshold — a fixed VIX > 20
  would give near-zero R1 days in 2017 and 2023).
- Verify R4 triggers on any day Nifty < −2% with no delay and no blending.

---

### B3b — Per-Stock Behavior Prior

**Files:**
- `weights/stock_type.py` — `StockTypePrior` class
  - Stores trend_alpha, trend_beta per stock symbol
  - `update(symbol, driver_cluster, score)` — updates posterior based on which cluster won
  - `get_modifier(symbol, cluster)` — returns cluster weight multiplier; 1.0 when n_eff < 15
- `checkpoints/stock_type_bayes.json` — written after every trade; 500 stocks × 2 params
- `backtester/composite_scorer.py` — apply `get_modifier()` when computing cluster scores

**Validation:** After 2016–2018 training, inspect 5 known stocks:
- HDFCBANK: expect trend_beta > trend_alpha (reversion-leaning)
- A momentum smallcap from watchlist: expect trend_alpha > trend_beta
- A neutral liquid large-cap: expect near 50/50

If 90% of stocks sit at 45–55% (near prior), n_eff < 15 and modifier is muted — this
is correct early behaviour. Modifier becomes meaningful after ~25 trades per stock.

---

### B3c — Hierarchical Cluster Priors

**Files:**
- `weights/bayesian.py` — extend `BayesianState`
  - Maintain pooled cluster posterior per cluster × direction (× regime); pooled α/β
    are the decayed sums of member-strategy evidence, updated on every trade
  - `get_posterior()` returns `mu_hier` per Section 3: strategy shrunk toward cluster
    with weight n_eff/(n_eff + K_HIER); K_HIER = 25 in `settings.py`
  - Shrinkage order: hierarchical first, then Phase 1 model-uncertainty shrinkage
- Signal log shows `mu_raw`, `mu_hier` (with w and cluster mean), and final `P_win`

**Validation:** After 2016–2018 training:
- A synthetic new strategy added to Cluster B mid-run starts near the cluster mean,
  not at 0.50
- ORB-15 (high n_eff) shows |mu_hier − mu_raw| < 0.02 — established strategies are
  barely affected
- Remove one strategy's trades and verify the cluster pool changes accordingly
  (no double counting)

---

### B3d — Bayesian Change-Point Detection

**Files:**
- `weights/changepoint.py` — `ChangePointMonitor` class
  - BOCPD (or CUSUM v1) per strategy × direction on the trade-score stream
  - Hazard prior 1/200 trades; alarm at p_change > 0.70 (`CHANGEPOINT_ALARM` in settings)
  - On alarm: temper posterior halfway toward Beta(3,3), log `[CHANGEPOINT ...]`
- Hooked into `BayesianState.update()` — runs after every settled trade
- Checkpoint: run-length state persisted in `checkpoints/changepoint_state.json`

**Validation:**
- Synthetic test: feed a strategy 100 trades at 62% win then flip to 40% — alarm must
  fire within 15 post-break trades, position size must drop within 5 trades of alarm
- Feed 200 stationary trades at 55% — no alarm (false positive check)
- Replay 2020 COVID onset: expect alarms on trend/breakout strategies in March 2020

---

### B4 — Build 16 New Strategies

All new strategies start at Beta(3,3). Driver-eligible per cluster assignment above.

**B4a — High priority (simple to implement, no new data sources):**
- `strategies/breakout/pwh_pwl.py` — prior week high/low breakout
- `strategies/breakout/ttm_squeeze.py` — Bollinger inside Keltner condition
- `strategies/structure/fvg.py` — 3-candle Fair Value Gap detection
- `strategies/structure/three_bar_rev.py` — Morning Star, Evening Star, 3WS, 3BC
- `strategies/context/fib_retracement.py` — swing-based 38.2/50/61.8 levels

**B4b — Medium priority (require indicator computation):**
- `strategies/reversion/rsi_divergence.py` — price vs RSI divergence
- `strategies/reversion/mfi_divergence.py` — Money Flow Index extreme + divergence
- `strategies/reversion/vol_price_div.py` — new price high + declining volume
- `strategies/reversion/keltner_rev.py` — price outside Keltner → reversion signal
- `strategies/trend/parabolic_sar.py` — dot flip signals trend reversal

**B4c — Meta signals (new data sources required: NSE PCR + block deals daily):**
- `strategies/meta/options_pcr.py` — daily Put-Call Ratio from NSE
- `strategies/meta/day_seasonality.py` — day-of-week + expiry week
- `strategies/meta/pre_expiry.py` — F&O expiry week momentum
- `strategies/meta/block_deal.py` — NSE block deal net direction before open

**Stock Earnings Skip (additional quality filter, built alongside B4c):**

Taking an intraday position on a stock that is reporting earnings today (or has a board
meeting for dividend/buyback/split) is a different risk profile — the stock can gap or
reverse sharply at any point on news confirmation. This is not a signal-quality problem;
it is a risk-category problem. No posterior or EV gate catches it.

```
if stock in nse_earnings_today or stock in nse_board_meeting_today:
    skip this stock regardless of signal quality — log: EARNINGS_SKIP
```

**Implementation:**
- `config/earnings_calendar.json` — NSE publishes board meeting dates on its website;
  scraped daily pre-market alongside BLOCK-DEAL data (same pipeline, B4c infrastructure)
- Checked in `backtester/engine.py` immediately after the macro event check, before
  any signal evaluation for this stock
- Signal log: `[EARNINGS_SKIP: HDFCBANK board_meeting]`
- For backtesting: NSE historical board meeting dates available via Bhavcopy archives

**Scope:** board meetings for results, dividend, buyback, split, rights issue —
**plus corporate-action ex-dates** (ex-split, ex-bonus, ex-rights) from the Phase 1
`corporate_actions.json` calendar: price levels and indicators computed across an
ex-date boundary are structurally distorted even with adjusted data, so the stock is
skipped on its ex-date. Log: `[EXDATE_SKIP: TATASTEEL ex-bonus]`.
Does NOT skip for general analyst day / investor presentations — those rarely move price.

**B4d — Multi-timeframe (15-min derived from existing 5-min Parquet — no new download):**
- `strategies/multiframe/mtf_orb.py` — ORB signal confirmed on 15-min chart
- `strategies/multiframe/mtf_ema.py` — EMA cross confirmed on 15-min chart

---

### B4e — Execution Quality Layer (Trigger level; third external review)

The Phase 1 gates answer "is this SETUP statistically worth taking?" Nothing yet
answers "is THIS entry, at THIS price, off THIS candle, a good execution of that
setup?" A fully confirmed ORB long entered 3 ATR past its trigger, under an unbroken
Yesterday High, off a bar that just printed a 3× rejection wick, is a good setup
executed badly. This layer grades the entry, at signal time.

**Formal gate order (adopted from the review, and now the engine's evaluation order
and log taxonomy):**
```
L1 Context   — macro-event skip, earnings/ex-date skip, day-type & breadth tags (B4f)
L2 Setup     — quality filters, cluster confirmation, driver_mu, EV (Phase 1)
L3 Trigger   — THIS LAYER: structure acceptance, entry efficiency, candle quality
L4 Execution — Kelly sizing, portfolio caps, margin, rounding, fills (Phase 1 §2i)
```
Levels are an ordering and a log schema; the soft-gate semantics inside each level
are unchanged — levels do not become new hard gates.

**Design rules, fixed up front:**
- **Size modifiers, not strategies and not new hard gates.** Every component outputs
  a value in [0,1] and multiplies into position size next to gate_mult. Exactly ONE
  hard veto exists on day one (extreme chase, below); everything else discounts.
- **Every scalar constant lives in settings.py, is frozen per WF window, and is
  sensitivity-tested (±25%) in Phase 3.** The level MENU and the component
  applicability map are pre-registered here; a ±25% sweep cannot perturb set-valued
  choices, so changing the menu or the map is a spec change that restarts the WF cycle.
- **Counterfactual logging is the promotion currency.** Every vetoed or skipped
  trade's hypothetical outcome is computed with the same fill model, in backtest AND
  live paper, and logged `[CF_EXEC component=... realized_R=...]`. Components are
  promoted (multiplier → veto) or demoted (sizing-active → log-only) at annual WF
  cycles only, based on the realized R of the sets they cut — never mid-window.
  Counterfactual outcomes evaluate the GATES, never the strategies: a simulated fill
  is not real evidence and never touches a posterior.

**Component 1 — Market-Structure Acceptance `ms` (sizing-active v1).**
The review's #1 priority — and the component with the most hand-set structure in it,
which is exactly why it ships as a multiplier, not the review's hard gate.
```
Level menu (pre-registered): PDH/PDL, PWH/PWL, CPR pivot, session VWAP,
  opening-range high/low (exists only from 09:45), latest confirmed 2-2 fractal swing.
  Only levels that exist at signal time are evaluated; VWAP participates from 09:35.
  First-candle-exempt strategies (GAP-*, PDH-PDL, CPR — Phase 1 B2) whose signal IS
  the level interaction are evaluated against the remaining menu only.

A level is ACCEPTED in the trade direction when ≥ 2 consecutive completed 5-min
closes lie beyond it, OR one close lies beyond it by > 0.3 × ATR(14, 5m).

blocking_level = nearest UNACCEPTED opposing level between entry and target
block_frac     = (blocking_level − entry) / (target − entry)

ms = 1.0                     no unaccepted opposing level, or block_frac > 0.66
ms = linear 0.25 → 1.0       block_frac 0.33 → 0.66          (A/C/G drivers)
ms = 0.25                    block_frac ≤ 0.33               (A/C/G drivers)
ms = max(0.50, above)        B drivers — reversion trades AT levels by design;
                             structure opposition discounts it, never crushes it
```
No hard veto in v1: the `[CF_EXEC]` record must first show the ms=0.25 set is
genuinely negative-R; promotion to a veto is an annual-cycle decision.

**Component 2 — Entry Efficiency / anti-chase `ee` (sizing-active v1; owns the single
day-one hard veto).** The review's Gap 2 (entry quality) and Gap 6 (exhaustion) are
one phenomenon — extension — measured twice; merged here.
```
ext       = |expected entry (next-bar open est.) − trigger price| / ATR(14, 5m)
ee_ext    = 1.0 at ext ≤ 1.0, linear → 0.25 at ext = 4.0        (all drivers)
HARD VETO : ext > 4.0 → [EXEC_VETO chase]  — the one rule that needs no evidence

ee_vwap   = 1.0 at |price − VWAP| ≤ 4 × ATR, linear → 0.5 at 8 × ATR   (A/C/G only —
            reversion enters extended moves by design)
ee_consec = 1.0 at ≤ 4 consecutive same-direction-as-trade 5-min candles,
            linear → 0.5 at 8                                          (A/C/G only)

ee = min(ee_ext, ee_vwap, ee_consec)    min, not product — three correlated measures
                                        of one latent extension; a product would
                                        charge the same fact three times
```
Continuous ramps throughout — never the review's 100/80/60/40/20 step bands, which
would reintroduce the exact threshold-cliff problem Phase 1's soft gates removed.

**Component 3 — Trigger-Candle Quality `cq` (sizing-active v1).**
Direction-aware, evaluated on the completed signal bar (known at decision time; fills
are next-bar open, so no lookahead):
```
body_frac = |C − O| / (H − L)
close_loc = (C − L) / (H − L) for LONG;  (H − C) / (H − L) for SHORT
opp_wick  = wick against trade direction / max(body, 0.1 × (H − L))

cq = clip(0.5 × body_frac + 0.5 × close_loc, 0.2, 1.0)
Extreme opposing rejection (opp_wick ≥ 2 AND bar range > 1.5 × ATR(14, 5m)) → cq = 0.2
```
Direction-awareness makes one formula serve both families: a hammer at the lower band
scores HIGH for a reversion long (the down-move was rejected — that is the fade
thesis) and LOW for a fresh short. No per-cluster special-casing.

**Component 4 — Momentum Quality `mq` (LOG-ONLY v1, all clusters).**
```
mq = mean of clipped components: ATR(5)/ATR(20) expansion, 3-bar/20-bar volume ratio
     (relative volume — the implementable core of the review's Gap 7), range
     expansion, closing efficiency (net move / path length over 6 bars)
```
Computed and logged on every signal, applied to nothing. It starts benched because it
carries the heaviest redundancy in the layer (VOL-SPIKE is already a Cluster A volume
strategy; TTM-SQUEEZE is compression→expansion; cq already contains volume and close
location) and because for reversion drivers the correct SIGN is unknown — strong
opposing momentum is simultaneously "falling knife" (bad fade) and "exhausted move"
(good fade). The `[CF_EXEC]`/tag record decides its promotion at an annual cycle.
Delivery % (also Gap 7) is rejected outright: published EOD, so same-day use would be
lookahead in backtest; only yesterday's value could ever be used — deferred.

**Combination and interaction with sizing:**
```
exec_mult = ms × ee × cq                    (mq excluded in v1)

if exec_mult < 0.25 → skip, log [EXEC_SKIP ms=.. ee=.. cq=..]
   — explicit and attributable, instead of letting a stacked token size die
     silently in the Phase 1 integer-rounding rule

risk_fraction = KELLY_FRACTION × Kelly × posterior_scale × gate_mult × exec_mult × context_mult
                (context_mult from B4f, 1.0 otherwise — this is the FULL canonical
                 sizing chain, mirrored in Phase 1 §2i; nothing else multiplies in)
```
Signal log: `exec: ms=1.00 ee=0.82 cq=0.71 (mq=0.90 log) → 0.58`
`[LOT_ROUND_SKIP]` events whose intended risk was reduced by exec_mult are tagged
`cause=exec_mult` — multiplier stacking must not hide behind the rounding rule.

**Trade-count budget (pre-registered, checked per training year — not pooled, because
extension and acceptance bind regime-dependently and a pooled number can hide one
gutted year):**
```
exec-layer cuts = EXEC_VETO + EXEC_SKIP + LOT_ROUND_SKIP(cause=exec_mult)

Budget: ≤ 25% of entered trades per year overall, AND ≤ 40% for any single cluster.
  The per-cluster guard exists because four of the five active checks bind mostly on
  A/C/G drivers — a global budget alone would let breakout posteriors starve quietly:
  fewer A trades → thinner n_eff → stronger shrinkage toward 0.5 → fewer A trades
  pass the EV gate → further starvation.

Pre-registered response if exceeded: raise the EXEC_SKIP floor 0.25 → 0.40 first,
then widen the chase veto 4.0 → 5.0 ATR. Nothing else moves mid-cycle.
```

**Files:**
- `backtester/exec_quality.py` — rolling per-stock level tracker (PDH/PWH/CPR/VWAP/
  opening range/swing state), acceptance evaluator, ee/cq/mq computation
- All state reproducible from data ≤ signal bar — determinism is an exit criterion
- Constants and the pre-registered level menu + applicability map in `settings.py`

**Validation:** run 2016–2018 training years with the layer active. Expected: modest
trade-count reduction within budget, average exec_mult of entered trades > 0.7, and
`[CF_EXEC]` outcomes for 100% of cuts. The chase veto must fire on a synthetic
5-ATR-extension case; ms must go to 0.25 on a synthetic just-below-PDH long.

---

### B4f — Context Layer v1 + Trade Tags (Level 1; third external review)

**What already exists at Level 1 — this step does not duplicate it:** the macro-event
calendar filter (Phase 1 B2), earnings/board-meeting/ex-date skip (B4c), regime
conditioning R1–R4 (B3), and Cluster F — which IS a context layer implemented as
learned voting strategies (PCR, DAY-SEASONALITY, PRE-EXPIRY, BLOCK-DEAL): posteriors
learn whether each context signal predicts anything instead of assuming it. The
review's "no context layer" claim was overstated; what is genuinely missing is below.

**Trade tags — recorded on EVERY entered trade AND every gated signal, from day one.**
These are the Phase 4b meta-learner's feature set, recorded now so the live track
record is usable later. All computable from data already in the system:
```
day_type    : gap % at open (open vs prior close); inside/outside day (daily OHLC)
breadth     : % of point-in-time Nifty 500 members with a positive day return at
              signal time ("advancers" — the single pre-registered definition;
              %-above-VWAP was considered and dropped: one definition, not two)
sector_rs   : candidate's sector index return since open minus Nifty return
daily_trend : close(t−1) vs daily EMA20(t−1) — prior-days-only, lookahead-free.
              The legitimate kernel of the review's Gap 8: nothing else in the
              system carries daily trend STATE (PDH/CPR are levels, not trend).
              Logged, never a multiplier.
time_bucket : T1 09:15–10:30 | T2 10:30–13:00 | T3 13:00–15:15   (review Gap 10)
```

**Exactly one sizing rule in v1 — breadth opposition:**
```
LONG  with advancers < 30%  → context_mult = 0.7
SHORT with advancers > 70%  → context_mult = 0.7
otherwise                     context_mult = 1.0
Constants frozen per WF window; sensitivity-tested in Phase 3.
```
context_mult is the final term of the canonical sizing chain (B4e combination line,
mirrored in Phase 1 §2i). It is logged under L4 — its input (breadth) is an L1 fact,
but it acts at sizing, and the log taxonomy records where things act.

**Pre-registered promotion rules (annual WF cycle only):**
- **Time-bucket posteriors** (the review's Gap 10 as literally proposed): enabled only
  when ≥ 3 strategies reach bucket n_eff ≥ 20 AND their bucket means separate by > 2%
  — sample size without effect size promotes noise dimensions (mirrors the B3 regime
  check). The arithmetic says this stays dormant for years: ≤ 500 trades/year across
  48 strategies × 2 directions × 3 buckets ≈ 1.7 trades/cell/year — and that is the
  honest outcome. Note: for time-localised strategies (ORB-15/30, FIRST-CANDLE,
  GAP-*) the bucket dimension is nearly degenerate with the strategy itself; only
  all-day strategies would ever use it.
- **day_type / sector_rs / daily_trend**: log-only until Phase 4b consumes them.
  No hand-set multipliers will be attached to them.

**Signal-level outcome label (pre-registered now, consumed later):**
```
For every signal that reaches engine evaluation (not every raw firing across 500
stocks):
  label = 1    direction-consistent move ≥ 0.5 × ATR(14, 5m) within 12 bars (1 hour)
  label = 0    opposite move ≥ 0.5 × ATR first
  label = 0.5  neither within 12 bars
The definition constants are pre-registered here and never tuned.
```
Logged from the B6 replay onward (computable for all of 2016–2026), written only
after the window resolves, read by no decision path. This label is the only route to
ever LEARNING confidence weights for clusters E and F — fixed at c = 1.0 in Phase 1
§2g because driver-only updates pin their posteriors at prior. Signal-level labels
are per-signal facts, not recycled trade PnL, so they carry none of the
pseudo-replication problem that rules out co-firing posterior updates. Any
consumption (learned c for E/F, a meta-learner feature) is an annual-cycle promotion
counted in the DSR trials.

**Deferred outright:** global-indices feed (new data source; overnight information is
already mostly captured by the gap % tag), delivery % (EOD-published — lookahead if
used same-day), news/NLP.

**Files:**
- `backtester/context_tags.py` — day-type classifier, breadth aggregator, sector RS,
  tag writer
- Breadth requires a per-bar universe aggregate over point-in-time membership
  (`nifty500_membership.json`, Phase 1 B2) — a real precompute pipeline
  (500 stocks × ~75 bars/day × 10 years), scheduled as data plumbing alongside the
  B4c scrapers, not a footnote

---

## 7. PHASE 2 EXIT CRITERIA

All must pass before starting Phase 3:

| Check | Requirement |
|---|---|
| B3 regime split | R1 vs R2 posterior means differ > 2% for ≥ 3 strategies |
| B3 hysteresis | No same-day regime flip in VIX-oscillation test periods |
| B3 blending | Monotone mu output across the P75→P85 VIX blend zone; no step discontinuity |
| B3 regime balance | R1 gets ≥ 15% of trading days in every calendar year 2016–2025 (percentile threshold working) |
| B3 no lookahead | VIX_R1_LEVEL(t) reproducible from data ≤ t−1 only, verified on spot dates |
| B3 R4 | Triggers same day as Nifty < −2%; no hysteresis delay |
| B3b stock prior | HDFCBANK shows trend_beta > trend_alpha after 2016–2018 training |
| B3b modifier | 90% of stocks near 50/50 at start (n_eff < 15) — confirms conservative prior |
| B3c hierarchy | New strategy in a cluster starts near cluster mean, not 0.50; high-n_eff strategies shifted < 0.02 |
| B3c logging | mu_raw, mu_hier, P_win all present in signal log |
| B3d change point | Synthetic 62%→40% break: alarm within 15 trades, size drop within 5 trades of alarm |
| B3d false positives | 200 stationary trades at 55%: zero alarms |
| B4 all 16 live | All new strategies fire at least once per 3-month training segment |
| B4 new strategy weights | New strategies start at cluster-informed prior (B3c); do not pass EV gate on prior alone |
| Composite score | Score increases after regime modifier wired in (vs flat Phase 1 scores) |
| B4e components | ms, ee, cq, mq and final exec_mult present in the log for every signal reaching L3; mq provably never affects size |
| B4e chase veto | `[EXEC_VETO chase]` fires on synthetic 5-ATR extension; no other hard veto exists anywhere in the layer |
| B4e structure gate | ms = 0.25 on synthetic just-below-PDH long (A driver); same setup with B driver gives ms = 0.50 |
| B4e counterfactual | 100% of EXEC_VETO / EXEC_SKIP cuts carry `[CF_EXEC]` hypothetical outcomes |
| B4e determinism | Acceptance state recomputed from data ≤ signal bar reproduces every ms decision (no lookahead) |
| B4e early session | Zero opening-range evaluations before 09:45; zero VWAP components before 09:35 |
| B4e trade budget | Per training year: exec-layer cuts ≤ 25% overall and ≤ 40% per cluster (including LOT_ROUND_SKIP cause=exec_mult) |
| B4f tags | All five tags present on 100% of entered trades and gated signals |
| B4f breadth | Computed against point-in-time Nifty 500 membership, not today's list |
| B4f sizing isolation | context_mult ∈ {0.7, 1.0} is the only context sizing effect; day_type/sector_rs/daily_trend provably log-only |
| B4f signal labels | Signal-level outcome label present for 100% of engine-evaluated signals; written post-resolution; read by no decision path |
| B3b decision-inert | stock_type modifier provably touches only the composite score — no gate, EV, ranking, or sizing path reads it |
