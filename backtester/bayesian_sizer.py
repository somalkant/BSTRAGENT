"""
B2 — Full-confirm flat-risk position sizing + portfolio limits (plan_phase1.md §2i)

Each direction (LONG/SHORT) carries its own capital allocation (LONG_CAPITAL /
SHORT_CAPITAL) and its own flat daily risk budget (DAILY_RISK_CAP_RS). This system
takes at most one long trade and one short trade per day, so "daily risk budget"
and "per-trade risk" are the same number. Once a signal has passed cluster
confirmation, driver confidence, EV and execution-quality gates, it is "as
confirmed as this system gets" and is sized to the FULL risk budget for its
direction — not shrunk further by a confidence-scaled (Kelly) fraction of it.
Those gates already decided WHETHER to enter; they don't also decide HOW MUCH.

    risk_amount = DAILY_RISK_CAP_RS                      (flat, once gate passed)
    notional    = risk_amount / stop_pct
    caps (in order) : MAX_STOCK_NOTIONAL (per-direction capital), LIQUIDITY (1% ADV),
                      MARGIN (SEBI 20% floor)
    integer rounding: shares = floor(notional/price); skip on >25% risk drift or 0 shares

Burn-in (an unproven driver still gathering evidence) is unchanged: a small fixed
token-exploration fraction of the direction's capital, never the full budget.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from config.settings import (
    CAPITAL, MAX_RISK_PER_TRADE, DAILY_RISK_CAP_RS,
    MAX_STOCK_NOTIONAL, MIS_LEVERAGE, SEBI_MARGIN_FLOOR, LIQUIDITY_ADV_CAP,
    ROUND_RISK_TOLERANCE, BURN_IN_RISK_FRACTION,
)

log = logging.getLogger(__name__)


@dataclass
class SizeResult:
    ok:            bool
    skip_reason:   str
    shares:        int
    notional:      float          # shares × entry
    intended_risk: float          # rupee risk before integer rounding
    actual_risk:   float          # shares × |entry-stop|
    risk_fraction: float          # intended_risk / capital
    kelly:         float          # diagnostic only: EV/RR, not used to size (see module docstring)
    flags:         list           # e.g. ["MARGIN_CAPPED", "LOT_ROUND_SKIP"]


def size_trade(
    entry: float, stop: float, rr: float, ev: float,
    *,
    capital: float = CAPITAL,
    adv_turnover_rs: float | None = None,      # 20-day avg daily turnover (Rs)
    available_cash: float | None = None,       # for the margin check
    existing_margin_used: float = 0.0,         # margin already committed by open positions
    margin_rate: float | None = None,          # max(VaR+ELM, 20%); default SEBI floor
    burn_in: bool = False,                     # unproven driver -> fixed token risk
) -> SizeResult:
    if entry <= 0 or stop <= 0 or entry == stop:
        return SizeResult(False, "bad-levels", 0, 0, 0, 0, 0, 0, [])

    stop_pct = abs(entry - stop) / entry
    kelly = max(0.0, ev / rr) if rr > 0 else 0.0   # diagnostic only

    if burn_in:
        # exploration size: a small fixed fraction, not the full confirmed budget
        risk_fraction = min(BURN_IN_RISK_FRACTION, MAX_RISK_PER_TRADE)
        intended_risk = capital * risk_fraction
    else:
        # full-confirm: the gate already required cluster confirmation, driver
        # confidence and EV to clear their bars -- size to the full direction budget
        intended_risk = min(DAILY_RISK_CAP_RS, capital)
        risk_fraction = intended_risk / capital if capital > 0 else 0.0
    if intended_risk <= 0:
        return SizeResult(False, "zero-risk", 0, 0, 0, 0, risk_fraction, kelly, [])

    notional = intended_risk / stop_pct
    flags: list[str] = []

    # notional caps (secondary sanity bounds)
    notional = min(notional, MAX_STOCK_NOTIONAL * capital)          # 100% of direction capital
    if adv_turnover_rs and adv_turnover_rs > 0:
        liq_cap = LIQUIDITY_ADV_CAP * adv_turnover_rs               # <= 1% of 20-day ADV
        if notional > liq_cap:
            notional = liq_cap
            flags.append("LIQUIDITY_CAPPED")

    # margin check (SEBI peak-margin floor); sum over open positions <= available cash
    cash = available_cash if available_cash is not None else capital
    mrate = margin_rate if margin_rate is not None else SEBI_MARGIN_FLOOR
    mrate = max(mrate, SEBI_MARGIN_FLOOR)
    max_notional_by_margin = max(0.0, (cash - existing_margin_used) / mrate)
    if notional > max_notional_by_margin:
        notional = max_notional_by_margin
        flags.append("MARGIN_CAPPED")

    # hard ceiling: never exceed intraday buying power (5x cash)
    notional = min(notional, MIS_LEVERAGE * capital)

    # What the notional caps above actually leave room to risk -- distinct from the
    # ORIGINAL intended_risk. A capital/liquidity/margin cap legitimately shrinking
    # the position (now common: the full flat budget can exceed 100% of a direction's
    # capital on a tight-stop name) is not the same failure as integer-share rounding
    # losing precision, and must not be punished the same way -- the trade should
    # still go through at whatever size the real constraint allows.
    capped_intended_risk = notional * stop_pct

    # integer-share rounding (checked last)
    shares = int(math.floor(notional / entry))
    actual_risk = shares * abs(entry - stop)
    if shares <= 0:
        flags.append("LOT_ROUND_SKIP")
        return SizeResult(False, "lot-round-zero-shares", 0, 0, intended_risk, 0,
                          risk_fraction, kelly, flags)
    if capped_intended_risk > 0:
        drift = abs(actual_risk - capped_intended_risk) / capped_intended_risk
        if drift > ROUND_RISK_TOLERANCE:
            flags.append("LOT_ROUND_SKIP")
            return SizeResult(False, "lot-round-risk-drift", shares, round(shares * entry, 2),
                              intended_risk, round(actual_risk, 2), risk_fraction, kelly, flags)

    return SizeResult(True, "", shares, round(shares * entry, 2), round(intended_risk, 2),
                      round(actual_risk, 2), risk_fraction, kelly, flags)


def sectors_ok(long_sector: str | None, short_sector: str | None) -> bool:
    """SECTOR_RULE: an open LONG and SHORT must be in different NSE sectors (§2i)."""
    if not long_sector or not short_sector:
        return True
    return long_sector != short_sector
