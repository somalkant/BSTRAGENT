# Trading Agent — System Validation Plan

## Purpose
This document tracks every known bug, its fix status, and the tests that prove the
system is working correctly before real-money trading begins.

Run the automated suite every morning before starting the agent:
```
python tests/validate_system.py
```
All 12 tests must show **PASS**. A single FAIL → do NOT start the agent that day.

---

## Known Bugs & Fix Status

| # | Bug | Root Cause | Fixed In | Status |
|---|-----|-----------|----------|--------|
| B1 | `signal_time=09:10` (pre-open bar) | Loop at 09:15 computes `bar_label = 09:15 − 5min = 09:10`; pre-open ticks become a fake bar | `live/agent.py` + `live/data_manager.py` | **Fixed 2026-06-16** |
| B2 | stop/target NOT distance-adjusted after live-price entry | Entry updated FIRST, then distances computed using already-updated entry → circular → stop/target unchanged | `live/live_engine.py` | **Fixed (prev session)** |
| B3 | `rr` field not updated after live-price entry | `rr` is a stored field in Signal dataclass, not computed property | `live/live_engine.py` | **Fixed (prev session)** |
| B4 | No intraday price monitoring after entry | Agent went silent from entry to exit — no 5-min P&L logs | `live/agent.py` (_log_trade_monitor) | **Fixed 2026-06-16** |
| B5 | Stale `.pyc` causes old code to run on restart | Python loads compiled bytecode from `__pycache__` if it exists | Pre-session checklist | **Mitigation added** |
| B6 | Liquidity filter (50 Cr) only takes effect after agent restart | `_LIQUIDITY_CR` computed at import time | Settings change requires restart | **By design — document** |
| B7 | Live SHORT has no per-symbol F&O eligibility gate | `live/live_engine.py` only checks the global `SHORT_ENABLED` flag. `backtester/universe.py::fno_eligible_short()` + `config/fno_membership.json` (a liquidity-proxy approximation built 2026-07-05, see `scripts/build_fno_proxy.py`) exist ONLY on the backtest path — confirmed zero references in `live/` or `brokers/`. Live can currently recommend a SHORT on a non-F&O stock with no warning. | Not fixed | **Open — must close before live shorting** |
| B8 | No broker order-placement / rejection handling exists | `brokers/base.py` defines only `authenticate()` / `get_credentials()` / `get_api_classes()` — no `place_order()` anywhere in the codebase. `live/agent.py` only writes to `paper_logger.py`; no real order is ever submitted, so there is nothing to catch a broker rejection. `plan_phase1.md`'s `[BROKER_REJECTED] log + skip, no retry` behavior is a **design note for future Phase 4a work**, not implemented code. | Not built (paper-trading only, by design for current phase) | **Open — required before "Start real money" milestone** |
| B9 | Backtest `simulate_execution()` never re-anchored stop/target to the actual (slipped) fill — could mislabel exits and invert PnL sign | `stop`/`target` used the raw signal-time absolute levels unchanged. When slippage/impact (`shares/bar_volume` participation) moved `entry_fill` far enough, the fill could land on the wrong side of the original target, so the exit-walk could tag a losing trade `TARGET` (found via a real DLF 2019-01-01 SHORT: entry slipped from 181.50 to 178.33 on a thin bar, later "hit" the stale 180.465 target level and was booked as `-3719, TARGET` — actually a loss). Same class of bug as B2/B3, just never ported from `live_engine.py` to the backtest engine. | `backtester/execution.py` — re-anchors stop/target to `entry_fill` preserving the signal's original R-distances; `tests/test_execution.py` updated to match | **Fixed 2026-07-05** — re-run any existing walk-forward checkpoints, their results predate this fix |
| B10 | Slippage/impact formula could imply a fill price that never actually traded in that bar | `entry_fill`/`exit_price` were computed as `open × (1 ± participation-based slip)` with no bound — on a thin bar (large order vs small bar volume) this can land outside the bar's own `[low, high]`. Same DLF trade: the 11:20 bar traded 181.35-181.75 the whole 5 minutes, but the formula implied a fill of 178.33 — ₹3.02 *below* the bar's own low, physically impossible. After B9's fix this was still producing an inflated loss (-5771, correctly labeled STOP but off a fill that couldn't have happened); after this fix the same trade fills at 181.35 (the bar's own low, the worst real price) and comes out **+260, a realistic small win**. | `backtester/execution.py` — added `_clamp()`, applied to every entry/exit fill | **Fixed 2026-07-05** — same re-run note as B9 |

---

## Pre-Session Checklist (run before 9:10 AM every day)

```powershell
# 1. Clear compiled bytecode so code changes always take effect
Remove-Item -Recurse -Force live\__pycache__, backtester\__pycache__, strategies\__pycache__, config\__pycache__ -ErrorAction SilentlyContinue

# 2. Run validation suite — ALL must pass
python tests/validate_system.py

# 3. If all pass, start the agent
python live/agent.py
```

---

## Test Categories

### V1 — Pre-Open Bar Guard
**What it tests:** The `bar_label < 09:15` guard in `_run_market_loop` prevents a 09:10-labeled
bar being created from pre-open tick accumulation.

**Why it matters:** Without this, the first "trading" bar contains NSE pre-open price data
(09:00–09:15) which is illiquid and uses a different price discovery mechanism. Strategies
see a false breakout at 09:10 and fire before the market actually opens.

**How it fails:** `signal_time=09:10` in the log — means a pre-open bar slipped through.

**Test in:** `tests/validate_system.py::test_preopen_bar_guard`

---

### V2 — Pre-Open Tick Filter
**What it tests:** `data_manager.on_tick()` discards ticks arriving before 09:15 IST.

**Why it matters:** Even if the bar guard is in place, pre-open ticks accumulate inside the
`CandleBuilder` open state. When the first real bar (09:15–09:20) starts, those ticks inflate
the open price and volume of that bar.

**Test in:** `tests/validate_system.py::test_preopen_tick_filter`

---

### V3 — Live-Price Entry: Distance Preservation
**What it tests:** When a signal fires at strategy entry Rs X but the live price is Rs Y,
the adjusted stop and target must be shifted by the same distances so R:R is unchanged.

**Correct formula:**
```
stop_dist   = entry_original − stop_original      # compute BEFORE updating entry
target_dist = target_original − entry_original
live_stop   = live_entry − stop_dist
live_target = live_entry + target_dist
```

**Bug that was there:** Entry was updated first, then `stop_dist = best_sig.entry − best_sig.stop`
used the ALREADY-UPDATED entry → stop_dist = live_entry − stop → same stop returned.

**DIXON example (2026-06-16 run with old code):**
- Strategy: entry=11933, stop=11445.15, target=13777, rr=3.78
- Live price: 12206
- Should get: stop=11718.15, target=14050, rr=3.78
- Got (old bug): stop=11445.15, target=13777, rr=2.06

**Test in:** `tests/validate_system.py::test_live_price_entry_distances`

---

### V4 — RR Field Consistency
**What it tests:** After live-price adjustment, `signal.rr` must equal
`(target − entry) / (entry − stop)` using the adjusted values.

**Why it matters:** `rr` is a stored float, NOT a computed property. If not updated after
entry/stop/target change, the RR shown in logs and CSV is wrong. Paper trade tracking
incorrectly shows trade quality.

**Test in:** `tests/validate_system.py::test_rr_consistency`

---

### V5 — Position Sizing
**What it tests:**
1. `risk_rs = (entry − stop) × shares ≤ MAX_LOSS_PER_TRADE × conviction_mult`
2. `position_rs = shares × entry ≤ MAX_POSITION_SIZE`
3. At least 1 share returned

**Config values:**
- `MAX_LOSS_PER_TRADE = Rs 20,000` (scales with conviction: 1x/1.5x/2x)
- `MAX_POSITION_SIZE = Rs 5,00,000`

**Test in:** `tests/validate_system.py::test_position_sizing`

---

### V6 — P&L Calculation
**What it tests:** `net_pnl(entry, exit, shares)` matches manual calculation including
all cost components: brokerage (Rs 40 RT), STT (0.025% sell), exchange (0.00345%/side),
SEBI (0.0001%/side), GST (18% on broker+exchange), stamp (0.003% buy), slippage (0.05%/side).

**Regression checks:**
- `net_pnl(price, price, shares) < 0` — breakeven gross means net loss from costs
- `net_pnl(entry, target, shares) > 0` — target hit must be profitable
- `net_pnl(entry, stop, shares) < -MAX_LOSS_PER_TRADE * 0.9` — stop loss within limits

**Test in:** `tests/validate_system.py::test_pnl_calculation`

---

### V7 — Quality Filters (All 6)
**What it tests:** Each filter individually rejects bad trades:
1. Liquidity < 50 Cr → reject
2. RR < 1.5 → reject
3. Agreement < 4 → reject
4. Circuit limit (>10% move from open) → reject
5. signal_time ≥ 14:00 → reject
6. After 10:30 AM requires score ≥ 7

**Test in:** `tests/validate_system.py::test_quality_filters`

---

### V8 — CSV Logger: Column Integrity
**What it tests:** A logged trade produces exactly 24 columns in correct order:
`date, symbol, signal_time, entry_time, strategy_entry, entry_price, quantity,
position_rs, stop_loss, target, rr, strategies_fired, agreeing_count, composite_score,
driver_strategy, reason, exit_time, exit_price, exit_reason, result, pnl_rs, pnl_pct,
predicted_win_pct, conviction_tier`

**Why it matters:** Dashboard reads these columns by name. A missing or reordered column
causes silent wrong values or KeyError in dashboard.

**Test in:** `tests/validate_system.py::test_csv_column_integrity`

---

### V9 — Checkpoint Save/Load (Crash Recovery)
**What it tests:** An open trade can be serialised to JSON and deserialised back with all
fields intact — including nested `signal` dict. Simulates agent restart mid-day.

**Critical fields to round-trip:** symbol, entry, stop, target, rr, signal_time,
entry_time, strategy_entry, shares, position_rs, agreeing.

**Test in:** `tests/validate_system.py::test_checkpoint_roundtrip`

---

### V10 — No Forward-Look: History Strictly Before Today
**What it tests:** `data_manager.load_history_from_parquet()` filters to
`datetime.dt.date < today`. No bar from today's date must appear in history.

**Why it matters:** If today's data leaks into history, strategies can "see" today's
price action before it happens — this inflates backtest win rates and causes look-ahead
bias in live signals.

**Test in:** `tests/validate_system.py::test_no_forward_look`

---

### V11 — Strategy Signal Validity
**What it tests:** Every one of the 27 strategies returns a valid `Signal` object
(no exception) when given minimal but valid input. Also checks:
- `direction` is one of {-1, 0, +1}
- If `direction != 0`: `entry > 0`, `stop < entry`, `target > entry`, `rr > 0`

**Why it matters:** A strategy crash during `scan_once()` is silently swallowed
(logged as DEBUG) — the strategy just returns direction=0. This means it effectively
drops out of the system without warning.

**Test in:** `tests/validate_system.py::test_strategy_signal_validity`

---

### V12 — CandleBuilder: OHLCV Correctness
**What it tests:** Feed a known sequence of (price, volume) ticks to a `CandleBuilder`,
close the bar, and verify:
- `open` = first tick price
- `high` = max tick price
- `low` = min tick price
- `close` = last tick price
- `volume` = cumulative day volume at close − cumulative at open

**Test in:** `tests/validate_system.py::test_candle_builder`

---

## What a Clean Run Looks Like

```
python tests/validate_system.py

Running Trading Agent System Validation
========================================
V1  Pre-Open Bar Guard ................ PASS
V2  Pre-Open Tick Filter .............. PASS
V3  Live-Price Entry Distances ......... PASS
V4  RR Field Consistency .............. PASS
V5  Position Sizing ................... PASS
V6  P&L Calculation ................... PASS
V7  Quality Filters ................... PASS
V8  CSV Column Integrity .............. PASS
V9  Checkpoint Round-Trip ............. PASS
V10 No Forward-Look in History ........ PASS
V11 Strategy Signal Validity .......... PASS (27/27 strategies)
V12 CandleBuilder OHLCV ............... PASS
========================================
12/12 passed   — system is CLEAR TO RUN
```

If any test fails:
```
V3  Live-Price Entry Distances ......... FAIL
    Expected live_stop=11718.15, got 11445.15
    Stop distance not preserved — live-price entry bug has returned
    Fix: check live/live_engine.py lines 128-148
```

---

## Road to Real Money

| Milestone | Condition |
|-----------|-----------|
| Paper trading | Currently running — B1-B6 fixed (B7/B8 don't block paper trading, no real orders are placed) |
| Consider real money | 30+ paper trades, win rate ≥ 45%, avg RR ≥ 1.8, all 12 validation tests green daily |
| Start real money | **B7 and B8 closed** (live F&O short gate + broker order-placement/rejection handling built) + begin with 25% of planned capital (Rs 2.5L), 1 trade/day, max Rs 5k loss |
| Scale up | After 20 real trades with win rate ≥ 45% |

The 30-trade paper threshold is important — with 1 trade/day that's ~6 weeks. Anything less
is not statistically meaningful (confidence interval on win rate is ±15% at 30 trades).
