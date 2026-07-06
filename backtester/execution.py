"""
B2 — Baseline execution realism + per-trade excursion record (plan_phase1.md B2)

Replaces the old signal-candle-close fill (look-ahead) with:
  entry = NEXT bar OPEN + slippage
  exits = touched level ± slippage; a bar that GAPS through the stop fills at the
          bar OPEN (worse than the stop), not the stop price
  EOD square-off at 15:10 (frozen)

Every computed fill (entry or exit) is clamped to that bar's own [low, high] — the
slippage/impact formula is a participation-rate estimate, not a real order book, and
on a thin bar (large order vs small bar volume) it can otherwise imply a price that
never actually traded that bar.

Every trade — winners included — records MFE_R / MAE_R (from 5-min bar extremes),
bars_to_exit, exit_reason ∈ {TARGET, STOP, EOD, CIRCUIT_TRAP}, settings_hash.
The full intra-bar path is unknowable, so excursions use bar highs/lows — same
convention in backtest and live paper. This record is log-only in Phase 1 (design
basis for the future exit project); no decision reads it.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import time as dtime

import pandas as pd

from config.settings import SLIPPAGE_BPS, SLIPPAGE_IMPACT_K, EOD_SQUAREOFF_TIME
from config.integrity import settings_hash

_SLIP_BASE = SLIPPAGE_BPS / 10000.0


@dataclass
class ExecResult:
    filled:        bool
    entry_fill:    float
    entry_time:    str
    exit_price:    float
    exit_time:     str
    exit_reason:   str        # TARGET / STOP / EOD / CIRCUIT_TRAP / NO_FILL
    bars_to_exit:  int
    mfe_r:         float      # max favourable excursion in R
    mae_r:         float      # max adverse excursion in R
    settings_hash: str

    def as_record(self) -> dict:
        return asdict(self)


def _slippage_frac(shares: int, bar_volume: float) -> float:
    """Base spread cost + a square-root market-impact term (see SLIPPAGE_IMPACT_K).
    Square-root, not linear: 10x the participation rate costs ~sqrt(10)=3.2x the
    impact, not 10x -- matches how real market impact scales, not a straight ratio."""
    part = (shares / bar_volume) if (shares and bar_volume and bar_volume > 0) else 0.0
    return _SLIP_BASE + SLIPPAGE_IMPACT_K * (part ** 0.5)


def _parse_time(s: str, default=(9, 15)) -> dtime:
    try:
        h, m = map(int, s.split(":"))
        return dtime(h, m)
    except Exception:
        return dtime(*default)


def _clamp(price: float, lo: float, hi: float) -> float:
    """No fill can occur outside the bar's own traded range — slippage/impact can
    push a raw formula price past what actually traded, which is not a real fill."""
    return min(max(price, lo), hi)


def simulate_execution(signal, today_5min: pd.DataFrame, shares: int = 0) -> ExecResult:
    """
    signal: has .direction (+1/-1), .entry, .target, .stop, .signal_time.
    Fills at the next bar's open; walks bars to the first of target/stop/EOD.

    stop/target are re-anchored to the actual fill, preserving the signal's original
    R-distances: entry_fill (next-bar-open, or slippage/impact when those are nonzero)
    can move far enough from signal.entry that the un-adjusted absolute levels end up
    on the wrong side of the real fill, which mislabels the exit and inverts the PnL
    sign (a "TARGET" tag that is actually a loss). Mirrors the fix already applied to
    live_engine.py for the same class of bug (VALIDATION_PLAN.md B2/B3).

    bayesian_engine.py deliberately computes TWO different risk bases from this
    result: a fill-gap safety check compares entry_fill against the ORIGINAL sig.stop
    (unaffected by this re-anchoring, to catch an entry that already gapped into
    trouble), while the risk basis used for Bayesian learning/reporting uses the
    signal's own geometric per-share risk (same basis as risk_per_share/mfe_r/mae_r
    below) — using the fill-gap distance there instead would make the posteriors
    learn from incidental signal-to-fill price drift, not genuine trade quality.
    """
    direction = signal.direction
    sh = settings_hash()

    sig_dt = _parse_time(signal.signal_time or "09:15")
    eod = EOD_SQUAREOFF_TIME

    bars = today_5min.reset_index(drop=True)
    # entry = first bar strictly AFTER the signal bar
    entry_idx = None
    for i, c in bars.iterrows():
        if pd.Timestamp(c["datetime"]).time() > sig_dt:
            entry_idx = i
            break
    if entry_idx is None:
        return ExecResult(False, 0.0, "", 0.0, "", "NO_FILL", 0, 0.0, 0.0, sh)

    ebar = bars.iloc[entry_idx]
    e_lo, e_hi = float(ebar["low"]), float(ebar["high"])
    slip = _slippage_frac(shares, float(ebar["volume"]))
    # buyer pays up, seller receives less — but never outside what the bar actually traded
    raw_fill = float(ebar["open"]) * (1 + slip) if direction > 0 else float(ebar["open"]) * (1 - slip)
    entry_fill = _clamp(raw_fill, e_lo, e_hi)
    entry_time = pd.Timestamp(ebar["datetime"]).strftime("%H:%M")

    # re-anchor stop/target to the real fill, preserving the signal's original R-distances
    stop_dist = abs(signal.entry - signal.stop)
    target_dist = abs(signal.target - signal.entry)
    if stop_dist <= 0 or target_dist <= 0:
        return ExecResult(False, entry_fill, entry_time, 0.0, "", "NO_FILL", 0, 0.0, 0.0, sh)
    if direction > 0:
        stop, target = entry_fill - stop_dist, entry_fill + target_dist
    else:
        stop, target = entry_fill + stop_dist, entry_fill - target_dist

    risk_per_share = stop_dist   # == abs(entry_fill - stop) by construction now

    best = 0.0   # best favourable move (price units)
    worst = 0.0  # worst adverse move (price units)
    exit_price = entry_fill
    exit_reason = "EOD"
    exit_time = entry_time
    bars_to_exit = 0

    # include the entry bar: we fill at its open, so its own high/low can trigger an exit
    for j in range(entry_idx, len(bars)):
        c = bars.iloc[j]
        t = pd.Timestamp(c["datetime"]).time()
        hi, lo, op = float(c["high"]), float(c["low"]), float(c["open"])
        bars_to_exit = j - entry_idx
        eslip = _slippage_frac(shares, float(c["volume"]))

        # update excursions from bar extremes
        if direction > 0:
            best = max(best, hi - entry_fill)
            worst = min(worst, lo - entry_fill)
        else:
            best = max(best, entry_fill - lo)
            worst = min(worst, entry_fill - hi)

        # EOD square-off (exits run to the frozen 15:10)
        if t >= eod:
            raw_exit = op * (1 - eslip) if direction > 0 else op * (1 + eslip)
            exit_price = _clamp(raw_exit, lo, hi)
            exit_reason, exit_time = "EOD", t.strftime("%H:%M")
            break

        if direction > 0:
            gap_through = op <= stop            # opened below the stop -> gap-through
            if lo <= stop:
                raw_exit = (op if gap_through else stop) * (1 - eslip)
                exit_price = _clamp(raw_exit, lo, hi)
                exit_reason = "CIRCUIT_TRAP" if gap_through and op < stop * 0.9 else "STOP"
                exit_time = t.strftime("%H:%M"); break
            if hi >= target:
                exit_price = _clamp(target * (1 - eslip), lo, hi)
                exit_reason, exit_time = "TARGET", t.strftime("%H:%M"); break
        else:
            gap_through = op >= stop
            if hi >= stop:
                raw_exit = (op if gap_through else stop) * (1 + eslip)
                exit_price = _clamp(raw_exit, lo, hi)
                exit_reason = "CIRCUIT_TRAP" if gap_through and op > stop * 1.1 else "STOP"
                exit_time = t.strftime("%H:%M"); break
            if lo <= target:
                exit_price = _clamp(target * (1 + eslip), lo, hi)
                exit_reason, exit_time = "TARGET", t.strftime("%H:%M"); break

    mfe_r = round(best / risk_per_share, 3)
    mae_r = round(worst / risk_per_share, 3)   # negative
    return ExecResult(True, round(entry_fill, 2), entry_time, round(exit_price, 2),
                      exit_time, exit_reason, bars_to_exit, mfe_r, mae_r, sh)
