"""
B2 — Capped fractional-Kelly position sizing + portfolio limits (plan_phase1.md §2i)

    Kelly           = EV / RR                         (EV from shrunk P(win))
    risk_fraction   = KELLY_FRACTION × Kelly × posterior_scale × gate_mult
    risk_amount     = capital × min(risk_fraction, MAX_RISK_PER_TRADE)
    notional        = risk_amount / stop_pct
    caps (in order) : MAX_STOCK_NOTIONAL, LIQUIDITY (1% ADV), MARGIN (SEBI 20% floor)
    integer rounding: shares = floor(notional/price); skip on >25% risk drift or 0 shares

The full sizing chain gains exec_mult (B4e) and context_mult (B4f) in Phase 2 — both
1.0 here. Portfolio-level daily-risk (0.8%) and the LONG/SHORT sector rule are enforced
by the engine across the day's two trades (helper: fit_daily_risk / sectors_ok).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from config.settings import (
    CAPITAL, KELLY_FRACTION, MAX_RISK_PER_TRADE, MAX_DAILY_RISK,
    MAX_STOCK_NOTIONAL, MIS_LEVERAGE, SEBI_MARGIN_FLOOR, LIQUIDITY_ADV_CAP,
    ROUND_RISK_TOLERANCE,
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
    risk_fraction: float
    kelly:         float
    flags:         list           # e.g. ["MARGIN_CAPPED", "LOT_ROUND_SKIP"]

    @property
    def risk_pct(self) -> float:
        return self.actual_risk / CAPITAL * 100.0


def size_trade(
    entry: float, stop: float, rr: float,
    ev: float, posterior_scale: float, gate_mult: float,
    *,
    capital: float = CAPITAL,
    adv_turnover_rs: float | None = None,      # 20-day avg daily turnover (Rs)
    available_cash: float | None = None,       # for the margin check
    existing_margin_used: float = 0.0,         # margin already committed by open positions
    margin_rate: float | None = None,          # max(VaR+ELM, 20%); default SEBI floor
    exec_mult: float = 1.0, context_mult: float = 1.0,   # Phase 2 terms (1.0 in Phase 1)
) -> SizeResult:
    if entry <= 0 or stop <= 0 or entry == stop:
        return SizeResult(False, "bad-levels", 0, 0, 0, 0, 0, 0, [])

    stop_pct = abs(entry - stop) / entry
    kelly = max(0.0, ev / rr) if rr > 0 else 0.0

    risk_fraction = KELLY_FRACTION * kelly * posterior_scale * gate_mult * exec_mult * context_mult
    risk_fraction = min(risk_fraction, MAX_RISK_PER_TRADE)          # 0.5%/trade cap
    intended_risk = capital * risk_fraction
    if intended_risk <= 0:
        return SizeResult(False, "zero-risk", 0, 0, 0, 0, risk_fraction, kelly, [])

    notional = intended_risk / stop_pct
    flags: list[str] = []

    # notional caps (secondary sanity bounds)
    notional = min(notional, MAX_STOCK_NOTIONAL * capital)          # 100% of cash per position
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

    # integer-share rounding (checked last)
    shares = int(math.floor(notional / entry))
    actual_risk = shares * abs(entry - stop)
    if shares <= 0:
        flags.append("LOT_ROUND_SKIP")
        return SizeResult(False, "lot-round-zero-shares", 0, 0, intended_risk, 0,
                          risk_fraction, kelly, flags)
    drift = abs(actual_risk - intended_risk) / intended_risk
    if drift > ROUND_RISK_TOLERANCE:
        flags.append("LOT_ROUND_SKIP")
        return SizeResult(False, "lot-round-risk-drift", shares, round(shares * entry, 2),
                          intended_risk, round(actual_risk, 2), risk_fraction, kelly, flags)

    return SizeResult(True, "", shares, round(shares * entry, 2), round(intended_risk, 2),
                      round(actual_risk, 2), risk_fraction, kelly, flags)


# ── portfolio-level helpers (engine enforces across the day's LONG + SHORT) ────
def fit_daily_risk(new_risk: float, risk_used_today: float, capital: float = CAPITAL) -> float:
    """
    Return the risk the new trade may use so the day stays within MAX_DAILY_RISK.
    Engine shrinks the second trade to fit, or skips if headroom is ~0.
    """
    headroom = MAX_DAILY_RISK * capital - risk_used_today
    return max(0.0, min(new_risk, headroom))


def sectors_ok(long_sector: str | None, short_sector: str | None) -> bool:
    """SECTOR_RULE: an open LONG and SHORT must be in different NSE sectors (§2i)."""
    if not long_sector or not short_sector:
        return True
    return long_sector != short_sector
