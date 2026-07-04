"""
B2 — Baseline execution realism + per-trade excursion record (plan_phase1.md B2)

Replaces the old signal-candle-close fill (look-ahead) with:
  entry = NEXT bar OPEN + slippage
  exits = touched level ± slippage; a bar that GAPS through the stop fills at the
          bar OPEN (worse than the stop), not the stop price
  EOD square-off at 15:10 (frozen)

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
    part = (shares / bar_volume) if (shares and bar_volume and bar_volume > 0) else 0.0
    return _SLIP_BASE + SLIPPAGE_IMPACT_K * part


def _parse_time(s: str, default=(9, 15)) -> dtime:
    try:
        h, m = map(int, s.split(":"))
        return dtime(h, m)
    except Exception:
        return dtime(*default)


def simulate_execution(signal, today_5min: pd.DataFrame, shares: int = 0) -> ExecResult:
    """
    signal: has .direction (+1/-1), .entry, .target, .stop, .signal_time.
    Fills at the next bar's open; walks bars to the first of target/stop/EOD.
    """
    direction = signal.direction
    target, stop = signal.target, signal.stop
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
    slip = _slippage_frac(shares, float(ebar["volume"]))
    # buyer pays up, seller receives less
    entry_fill = float(ebar["open"]) * (1 + slip) if direction > 0 else float(ebar["open"]) * (1 - slip)
    entry_time = pd.Timestamp(ebar["datetime"]).strftime("%H:%M")

    risk_per_share = abs(entry_fill - stop)
    if risk_per_share <= 0:
        return ExecResult(False, entry_fill, entry_time, 0.0, "", "NO_FILL", 0, 0.0, 0.0, sh)

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
            exit_price = op * (1 - eslip) if direction > 0 else op * (1 + eslip)
            exit_reason, exit_time = "EOD", t.strftime("%H:%M")
            break

        if direction > 0:
            gap_through = op <= stop            # opened below the stop -> gap-through
            if lo <= stop:
                exit_price = (op if gap_through else stop) * (1 - eslip)
                exit_reason = "CIRCUIT_TRAP" if gap_through and op < stop * 0.9 else "STOP"
                exit_time = t.strftime("%H:%M"); break
            if hi >= target:
                exit_price = target * (1 - eslip)
                exit_reason, exit_time = "TARGET", t.strftime("%H:%M"); break
        else:
            gap_through = op >= stop
            if hi >= stop:
                exit_price = (op if gap_through else stop) * (1 + eslip)
                exit_reason = "CIRCUIT_TRAP" if gap_through and op > stop * 1.1 else "STOP"
                exit_time = t.strftime("%H:%M"); break
            if lo <= target:
                exit_price = target * (1 + eslip)
                exit_reason, exit_time = "TARGET", t.strftime("%H:%M"); break

    mfe_r = round(best / risk_per_share, 3)
    mae_r = round(worst / risk_per_share, 3)   # negative
    return ExecResult(True, round(entry_fill, 2), entry_time, round(exit_price, 2),
                      exit_time, exit_reason, bars_to_exit, mfe_r, mae_r, sh)
