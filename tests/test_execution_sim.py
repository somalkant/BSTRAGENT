"""
B5b validation — execution simulator + circuit-lock (plan_phase3.md §B5b).

Run:  python -m pytest tests/test_execution_sim.py -v
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtester.execution_sim import (
    simulate, liquidity_tier, spread_frac, is_circuit_locked, SimResult,
)
from config.settings import SIM_CIRCUIT_PENALTY
from strategies.base import Signal


def _bars(rows, vol=1_000_000):
    return pd.DataFrame([
        {"datetime": pd.Timestamp(f"2020-03-12 {t}:00"),
         "open": o, "high": h, "low": lo, "close": c, "volume": v}
        for (t, o, h, lo, c, *rest) in rows
        for v in [rest[0] if rest else vol]])


def test_liquidity_tiers():
    assert liquidity_tier(200e7) == "large"
    assert liquidity_tier(50e7) == "mid"
    assert liquidity_tier(5e7) == "small"


def test_spread_widens_at_open_and_smallcap():
    assert spread_frac("small") > spread_frac("large")
    assert spread_frac("mid", at_open=True) > spread_frac("mid", at_open=False)
    assert spread_frac("mid", event_day=True) > spread_frac("mid")


def test_target_hit_with_tier_slippage():
    bars = _bars([("09:15", 100, 100, 100, 100), ("09:20", 100, 101, 99.8, 100.5),
                  ("09:25", 100.5, 106.5, 100.4, 106)])
    sig = Signal("X", +1, entry=100, target=106, stop=98, rr=3, signal_time="09:15")
    r = simulate(sig, bars, shares=100, turnover_rs=200e7)
    assert r.exit_reason == "TARGET" and r.tier == "large"
    assert r.exit_price < 106                       # slippage on exit


def test_gap_through_stop_fills_at_open():
    bars = _bars([("09:15", 100, 100, 100, 100), ("09:20", 95, 95.2, 94, 94.5)])  # gaps below stop 98
    sig = Signal("X", +1, entry=100, target=106, stop=98, rr=3, signal_time="09:15")
    r = simulate(sig, bars, shares=10, turnover_rs=200e7)
    assert r.exit_reason == "STOP"
    assert r.exit_price < 98                         # filled at the gapped-open, worse than stop


def test_partial_fill_flag_on_high_participation():
    # order 300k shares vs 1M bar volume = 30% > 20% cap -> partial
    bars = _bars([("09:15", 100, 100, 100, 100), ("09:20", 100, 100.5, 99.5, 100),
                  ("09:25", 100, 100.5, 99.5, 100)])
    sig = Signal("X", +1, entry=100, target=110, stop=90, rr=2, signal_time="09:15")
    r = simulate(sig, bars, shares=300_000, turnover_rs=200e7)
    assert r.partial is True


def test_circuit_lock_detection():
    locked = pd.Series({"high": 95.0, "low": 95.0, "volume": 0})
    normal = pd.Series({"high": 96.0, "low": 94.0, "volume": 1000})
    assert is_circuit_locked(locked) is True
    assert is_circuit_locked(normal) is False


def test_short_circuit_trap_auction_penalty_uncapped():
    # short entered, then a locked upper band (H==L, vol 0) through EOD -> auction penalty
    rows = [("09:15", 100, 100, 100, 100, 1_000_000),
            ("09:20", 100, 100.2, 99.8, 100, 1_000_000)]
    t = pd.Timestamp("2020-03-12 09:25:00")
    while t.strftime("%H:%M") < "15:15":
        rows.append((t.strftime("%H:%M"), 110, 110, 110, 110, 0))    # locked upper band
        t += pd.Timedelta(minutes=5)
    bars = pd.DataFrame([{"datetime": pd.Timestamp(f"2020-03-12 {r[0]}:00"),
                          "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                         for r in rows])
    sig = Signal("X", -1, entry=100, target=94, stop=102, rr=2, signal_time="09:15")
    r = simulate(sig, bars, shares=100, turnover_rs=200e7, next_day_open=115)
    assert r.circuit_trap is True and r.exit_reason == "CIRCUIT_TRAP"
    assert r.realized_r < -1.5                       # loss is UNCAPPED (worse than winsor floor)
