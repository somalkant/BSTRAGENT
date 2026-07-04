"""
B2 sizing validation — capped fractional Kelly + portfolio caps (plan_phase1.md §2i).

Run:  python -m pytest tests/test_bayesian_sizer.py -v
"""
from __future__ import annotations

import pytest

from backtester.bayesian_sizer import size_trade, fit_daily_risk, sectors_ok
from config.settings import (
    CAPITAL, MAX_RISK_PER_TRADE, MAX_DAILY_RISK, MIS_LEVERAGE, ROUND_RISK_TOLERANCE,
)


def test_per_trade_risk_cap_binds_and_never_exceeded():
    """High-EV trade: Kelly output is capped at 0.5%/trade; floor rounding keeps actual <= intended."""
    r = size_trade(entry=100.0, stop=98.0, rr=1.85, ev=0.83,
                   posterior_scale=0.8, gate_mult=1.0)
    assert r.ok
    assert r.risk_fraction == pytest.approx(MAX_RISK_PER_TRADE)     # cap binds
    assert r.actual_risk <= r.intended_risk + 1e-6                  # floor never rounds up
    assert r.risk_pct <= 0.5 + 1e-9                                 # hard 0.5% cap


def test_actual_risk_never_exceeds_half_percent_across_prices():
    for price in (50, 100, 250, 1300, 3000):
        stop = price * 0.98
        r = size_trade(entry=float(price), stop=stop, rr=1.85, ev=0.9,
                       posterior_scale=1.0, gate_mult=1.0)
        if r.ok:
            assert r.risk_pct <= 0.5 + 1e-9
            assert r.notional <= MIS_LEVERAGE * CAPITAL + 1e-6      # never > 5x cash


def test_token_size_from_low_gate_mult():
    """A borderline setup (small gate_mult) produces a small but nonzero position."""
    r = size_trade(entry=100.0, stop=98.0, rr=1.85, ev=0.20,
                   posterior_scale=0.3, gate_mult=0.1)
    assert r.risk_fraction < MAX_RISK_PER_TRADE
    if r.ok:
        assert r.actual_risk < 0.5 / 100 * CAPITAL


def test_lot_round_skip_high_priced_stock():
    """MRF-like price with token risk -> 0 shares or >25% drift -> LOT_ROUND_SKIP."""
    # intended risk tiny (gate_mult 0.05), stop 0.5% -> notional small vs 130000 price
    r = size_trade(entry=130000.0, stop=129350.0, rr=1.85, ev=0.20,
                   posterior_scale=0.3, gate_mult=0.05)
    assert not r.ok
    assert "LOT_ROUND_SKIP" in r.flags


def test_lot_round_risk_drift_bounded_when_ok():
    r = size_trade(entry=100.0, stop=99.0, rr=1.85, ev=0.83,
                   posterior_scale=1.0, gate_mult=1.0)
    if r.ok:
        drift = abs(r.actual_risk - r.intended_risk) / r.intended_risk
        assert drift <= ROUND_RISK_TOLERANCE


def test_margin_capped_synthetic_tight_stop():
    """0.1% stop at 0.5% risk with constrained cash -> MARGIN_CAPPED; notional <= 5x cash."""
    r = size_trade(entry=100.0, stop=99.9, rr=1.85, ev=0.9,
                   posterior_scale=1.0, gate_mult=1.0,
                   available_cash=1_00_000, margin_rate=0.20)   # 1L cash -> max notional 5L
    assert "MARGIN_CAPPED" in r.flags
    assert r.notional <= MIS_LEVERAGE * CAPITAL + 1e-6


def test_liquidity_cap():
    """Illiquid name: notional clamped to 1% of 20-day ADV turnover."""
    r = size_trade(entry=100.0, stop=98.0, rr=1.85, ev=0.9,
                   posterior_scale=1.0, gate_mult=1.0,
                   adv_turnover_rs=1_00_000)     # 1% -> 1000 notional cap
    assert "LIQUIDITY_CAPPED" in r.flags
    assert r.notional <= 0.01 * 1_00_000 + 100  # <= cap (plus one share rounding)


def test_daily_risk_headroom():
    used = 0.006 * CAPITAL                    # already used 0.6%
    fitted = fit_daily_risk(0.005 * CAPITAL, used)   # want another 0.5%
    assert fitted == pytest.approx(MAX_DAILY_RISK * CAPITAL - used)   # shrunk to 0.2%
    assert fit_daily_risk(0.005 * CAPITAL, MAX_DAILY_RISK * CAPITAL) == 0.0


def test_sector_rule():
    assert sectors_ok("IT", "PHARMA") is True
    assert sectors_ok("BANK", "BANK") is False
    assert sectors_ok(None, "BANK") is True     # only one side open
