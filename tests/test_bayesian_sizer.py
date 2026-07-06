"""
B2 sizing validation — full-confirm flat risk budget + portfolio caps (plan_phase1.md §2i).

Run:  python -m pytest tests/test_bayesian_sizer.py -v
"""
from __future__ import annotations

import pytest

from backtester.bayesian_sizer import size_trade, sectors_ok
from config.settings import (
    DAILY_RISK_CAP_RS, MIS_LEVERAGE, ROUND_RISK_TOLERANCE, BURN_IN_RISK_FRACTION,
)

DIRECTION_CAPITAL = 5_00_000   # mirrors LONG_CAPITAL/SHORT_CAPITAL for these unit tests


def test_full_confirm_sizes_to_the_flat_daily_risk_cap():
    """Once a signal passes the gate (non-burn-in), it sizes to the FULL per-direction
    budget -- not a Kelly/confidence-scaled fraction of it. 5% stop keeps the implied
    notional (20,000/0.05=400,000) under the 100%-of-capital cap (500,000) so the
    budget is fully realized, not just targeted."""
    r = size_trade(entry=100.0, stop=95.0, rr=1.85, ev=0.20, capital=DIRECTION_CAPITAL)
    assert r.ok
    assert r.intended_risk == pytest.approx(DAILY_RISK_CAP_RS)
    assert r.actual_risk == pytest.approx(DAILY_RISK_CAP_RS)
    assert r.risk_fraction == pytest.approx(DAILY_RISK_CAP_RS / DIRECTION_CAPITAL)


def test_tight_stop_hits_notional_cap_and_shrinks_gracefully_instead_of_skipping():
    """A 2% stop would need 20,000/0.02=1,000,000 notional to realize the full budget
    -- double the 500,000 (100%-of-capital) cap. The position must still go through
    at whatever the cap allows (liquidity/capital legitimately limited it), not get
    thrown away as if it were an integer-rounding failure."""
    r = size_trade(entry=100.0, stop=98.0, rr=1.85, ev=0.20, capital=DIRECTION_CAPITAL)
    assert r.ok
    assert r.notional == pytest.approx(DIRECTION_CAPITAL)          # capped at 100% of capital
    assert r.actual_risk == pytest.approx(DIRECTION_CAPITAL * 0.02)  # < the 20,000 target, and that's fine
    assert r.actual_risk < r.intended_risk


def test_full_confirm_size_is_the_same_regardless_of_ev_strength():
    """A barely-passing setup and a very strong one must size identically -- the gate
    already decided whether to enter; EV no longer decides how much once it has."""
    weak = size_trade(entry=100.0, stop=98.0, rr=1.85, ev=0.16, capital=DIRECTION_CAPITAL)
    strong = size_trade(entry=100.0, stop=98.0, rr=1.85, ev=0.90, capital=DIRECTION_CAPITAL)
    assert weak.intended_risk == pytest.approx(strong.intended_risk)
    assert weak.shares == strong.shares


def test_actual_risk_never_exceeds_flat_cap_across_prices():
    for price in (50, 100, 250, 1300, 3000):
        stop = price * 0.98
        r = size_trade(entry=float(price), stop=stop, rr=1.85, ev=0.9, capital=DIRECTION_CAPITAL)
        if r.ok:
            assert r.actual_risk <= DAILY_RISK_CAP_RS + 1e-6
            assert r.notional <= MIS_LEVERAGE * DIRECTION_CAPITAL + 1e-6   # never > 5x cash


def test_burn_in_uses_small_token_size_not_the_full_budget():
    """An unproven driver (burn_in=True) still explores at a small fixed fraction,
    not the full confirmed risk budget."""
    r = size_trade(entry=100.0, stop=98.0, rr=1.85, ev=0.20,
                   capital=DIRECTION_CAPITAL, burn_in=True)
    assert r.intended_risk < DAILY_RISK_CAP_RS
    assert r.intended_risk == pytest.approx(BURN_IN_RISK_FRACTION * DIRECTION_CAPITAL)


def test_lot_round_skip_high_priced_stock_in_burn_in():
    """MRF-like price with token (burn-in) risk -> 0 shares -> LOT_ROUND_SKIP."""
    r = size_trade(entry=130000.0, stop=129350.0, rr=1.85, ev=0.20,
                   capital=DIRECTION_CAPITAL, burn_in=True)
    assert not r.ok
    assert "LOT_ROUND_SKIP" in r.flags


def test_lot_round_risk_drift_bounded_when_ok():
    """5% stop keeps the implied notional under the capital cap, so any remaining
    drift is purely integer-share rounding (the thing this check is meant to catch)."""
    r = size_trade(entry=100.0, stop=95.0, rr=1.85, ev=0.83, capital=DIRECTION_CAPITAL)
    if r.ok:
        drift = abs(r.actual_risk - r.intended_risk) / r.intended_risk
        assert drift <= ROUND_RISK_TOLERANCE


def test_margin_capped_synthetic_tight_stop():
    """Constrained cash (well below the direction's own capital, e.g. margin already
    committed elsewhere) -> MARGIN_CAPPED binds before the notional cap; notional <= 5x cash."""
    r = size_trade(entry=100.0, stop=99.9, rr=1.85, ev=0.9, capital=DIRECTION_CAPITAL,
                   available_cash=50_000, margin_rate=0.20)   # 50K cash -> max notional 250K
    assert "MARGIN_CAPPED" in r.flags
    assert r.notional <= MIS_LEVERAGE * DIRECTION_CAPITAL + 1e-6


def test_liquidity_cap():
    """Illiquid name: notional clamped to 1% of 20-day ADV turnover."""
    r = size_trade(entry=100.0, stop=98.0, rr=1.85, ev=0.9, capital=DIRECTION_CAPITAL,
                   adv_turnover_rs=1_00_000)     # 1% -> 1000 notional cap
    assert "LIQUIDITY_CAPPED" in r.flags
    assert r.notional <= 0.01 * 1_00_000 + 100  # <= cap (plus one share rounding)


def test_sector_rule():
    assert sectors_ok("IT", "PHARMA") is True
    assert sectors_ok("BANK", "BANK") is False
    assert sectors_ok(None, "BANK") is True     # only one side open
