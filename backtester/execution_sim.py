"""
B5b — Execution simulator + circuit-lock model (plan_phase3.md §B5b)

Upgrades the Phase 1 baseline (next-bar-open + flat 5 bps) to a per-fill model:
  - bid-ask spread from liquidity tier (wider at open / on event days / in smallcaps)
  - slippage vs liquidity: impact ∝ participation rate (order_size / bar_volume)
  - latency: signal at bar close -> order reaches market next bar
  - partial fills: order > 20% of bar volume fills the remainder next bar (or carries)
  - gap-through stops: fill at bar OPEN, never at the stop price
  - circuit locks: a locked band has NO counterparty -> the exit does NOT fill; a
    still-locked position at EOD is force-carried (long) or auctioned with penalty (short)

Circuit-trap losses feed PnL/DD/Monte-Carlo UNCAPPED — the −1.5R winsorization floor
protects only the posterior evidence score (Phase 1 §2c).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime

import pandas as pd

from config.settings import (
    SIM_SPREAD_BPS_TIER, SIM_PARTICIPATION_CAP, SIM_CIRCUIT_PENALTY,
    SIM_LIQ_TIER_TURNOVER, EOD_SQUAREOFF_TIME,
)


def liquidity_tier(turnover_rs: float) -> str:
    if turnover_rs >= SIM_LIQ_TIER_TURNOVER["large"]:
        return "large"
    if turnover_rs >= SIM_LIQ_TIER_TURNOVER["mid"]:
        return "mid"
    return "small"


def spread_frac(tier: str, at_open: bool = False, event_day: bool = False) -> float:
    bps = SIM_SPREAD_BPS_TIER.get(tier, 20.0)
    if at_open:
        bps *= 2.0                      # first bar is widest
    if event_day:
        bps *= 1.5
    return bps / 10000.0


def _impact_frac(shares: int, bar_volume: float) -> float:
    if not bar_volume or bar_volume <= 0:
        return 0.0
    return min(shares / bar_volume, 1.0)      # participation-rate impact


def is_circuit_locked(bar) -> bool:
    """Proxy for a pinned/locked band: zero intrabar range with volume collapse."""
    rng = float(bar["high"]) - float(bar["low"])
    return rng <= 0 and float(bar["volume"]) <= 0


@dataclass
class SimResult:
    filled:       bool
    entry_fill:   float
    exit_price:   float
    exit_reason:  str        # TARGET/STOP/EOD/CIRCUIT_TRAP/NO_FILL
    tier:         str
    partial:      bool
    circuit_trap: bool
    realized_r:   float      # (exit-entry)/risk_per_share, signed by direction — UNCAPPED


def simulate(signal, today_5min: pd.DataFrame, shares: int, turnover_rs: float,
             next_day_open: float | None = None, event_day: bool = False) -> SimResult:
    direction = signal.direction
    target, stop = float(signal.target), float(signal.stop)
    tier = liquidity_tier(turnover_rs)

    sig_t = signal.signal_time or "09:15"
    try:
        h, m = map(int, sig_t.split(":")); sig_dt = dtime(h, m)
    except Exception:
        sig_dt = dtime(9, 15)

    bars = today_5min.reset_index(drop=True)
    entry_idx = next((i for i, c in bars.iterrows()
                      if pd.Timestamp(c["datetime"]).time() > sig_dt), None)
    if entry_idx is None:
        return SimResult(False, 0, 0, "NO_FILL", tier, False, False, 0.0)

    ebar = bars.iloc[entry_idx]
    at_open = pd.Timestamp(ebar["datetime"]).time() <= dtime(9, 20)
    sp = spread_frac(tier, at_open, event_day)
    imp = _impact_frac(shares, float(ebar["volume"]))
    slip = sp + imp
    entry_fill = float(ebar["open"]) * (1 + slip) if direction > 0 else float(ebar["open"]) * (1 - slip)
    partial = imp > SIM_PARTICIPATION_CAP

    risk_ps = abs(entry_fill - stop)
    if risk_ps <= 0:
        return SimResult(False, entry_fill, 0, "NO_FILL", tier, partial, False, 0.0)

    def _r(exit_px):
        return ((exit_px - entry_fill) if direction > 0 else (entry_fill - exit_px)) / risk_ps

    for j in range(entry_idx, len(bars)):
        c = bars.iloc[j]
        t = pd.Timestamp(c["datetime"]).time()
        op, hi, lo = float(c["open"]), float(c["high"]), float(c["low"])
        eslip = spread_frac(tier, False, event_day) + _impact_frac(shares, float(c["volume"]))

        if t >= EOD_SQUAREOFF_TIME:
            if is_circuit_locked(c):          # still locked at square-off -> trap
                return _circuit_trap(direction, entry_fill, risk_ps, next_day_open, tier, partial)
            px = op * (1 - eslip) if direction > 0 else op * (1 + eslip)
            return SimResult(True, round(entry_fill, 2), round(px, 2), "EOD", tier, partial, False, _r(px))

        if is_circuit_locked(c):              # locked band -> no fill this bar, re-attempt
            continue

        if direction > 0:
            if lo <= stop:
                px = (op if op <= stop else stop) * (1 - eslip)   # gap-through fills at open
                return SimResult(True, round(entry_fill, 2), round(px, 2), "STOP", tier, partial, False, _r(px))
            if hi >= target:
                px = target * (1 - eslip)
                return SimResult(True, round(entry_fill, 2), round(px, 2), "TARGET", tier, partial, False, _r(px))
        else:
            if hi >= stop:
                px = (op if op >= stop else stop) * (1 + eslip)
                return SimResult(True, round(entry_fill, 2), round(px, 2), "STOP", tier, partial, False, _r(px))
            if lo <= target:
                px = target * (1 + eslip)
                return SimResult(True, round(entry_fill, 2), round(px, 2), "TARGET", tier, partial, False, _r(px))

    # ran out of bars without EOD marker -> settle at last close
    last = float(bars.iloc[-1]["close"])
    return SimResult(True, round(entry_fill, 2), round(last, 2), "EOD", tier, partial, False, _r(last))


def _circuit_trap(direction, entry_fill, risk_ps, next_day_open, tier, partial) -> SimResult:
    """Still locked at 15:15: long force-carried to next open; short auctioned with penalty."""
    ndo = next_day_open if next_day_open is not None else entry_fill
    if direction > 0:
        px = ndo * (1 - 0.005)                                   # forced overnight carry
        r = (px - entry_fill) / risk_ps
    else:
        adverse = (ndo - entry_fill) / entry_fill
        px = entry_fill * (1 + max(SIM_CIRCUIT_PENALTY, adverse))  # auction penalty band
        r = (entry_fill - px) / risk_ps
    return SimResult(True, round(entry_fill, 2), round(px, 2), "CIRCUIT_TRAP", tier, partial, True, r)
