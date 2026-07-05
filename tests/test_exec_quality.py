"""
B4e validation — execution-quality layer (plan_phase2.md §B4e, exit criteria).

Run:  python -m pytest tests/test_exec_quality.py -v
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtester.exec_quality import compute
from strategies.base import Signal


def _bars(rows):
    return pd.DataFrame([
        {"datetime": pd.Timestamp(f"2018-06-01 {t}:00"),
         "open": o, "high": h, "low": lo, "close": c, "volume": 100000}
        for (t, o, h, lo, c) in rows])


def _times(n, start_h=9, start_m=15):
    out = []
    m = start_h * 60 + start_m
    for _ in range(n):
        out.append(f"{m//60:02d}:{m%60:02d}")
        m += 5
    return out


def _prev_day(high, low, close):
    return pd.Series({"high": high, "low": low, "close": close, "open": (high + low) / 2, "volume": 1})


# ── ms: market-structure acceptance ─────────────────────────────────────────

def test_ms_quarter_on_just_below_pdh_long_A_driver():
    ts = _times(5)
    rows = [(t, 99.6, 100.0, 99.4, 99.8) for t in ts]     # tight range below 100, PDH unaccepted
    bars = _bars(rows)
    sig = Signal("ORB-15", +1, entry=100.0, target=106.0, stop=98.0, rr=3.0, signal_time=ts[-1])
    prev = _prev_day(high=102.0, low=97.0, close=100.0)     # PDH 102 sits at block_frac 0.33
    eq = compute(sig, bars, prev, driver_cluster="A")
    assert eq.ms == pytest.approx(0.25, abs=0.01)


def test_ms_floor_half_for_B_driver_same_setup():
    ts = _times(5)
    bars = _bars([(t, 99.6, 100.0, 99.4, 99.8) for t in ts])
    sig = Signal("BOLLINGER", +1, entry=100.0, target=106.0, stop=98.0, rr=3.0, signal_time=ts[-1])
    prev = _prev_day(102.0, 97.0, 100.0)
    eq = compute(sig, bars, prev, driver_cluster="B")
    assert eq.ms == pytest.approx(0.50, abs=0.01)          # reversion floor


def test_ms_full_when_no_opposing_level():
    ts = _times(5)
    bars = _bars([(t, 99.6, 100.0, 99.4, 99.8) for t in ts])
    sig = Signal("ORB-15", +1, entry=100.0, target=101.0, stop=98.0, rr=1.0, signal_time=ts[-1])
    prev = _prev_day(120.0, 97.0, 100.0)                   # PDH 120 is beyond target -> no block
    eq = compute(sig, bars, prev, driver_cluster="A")
    assert eq.ms == 1.0


# ── ee: the one hard veto ────────────────────────────────────────────────────

def test_chase_veto_on_5atr_extension():
    ts = _times(7)
    # price ramps ~5 points over 6 bars with ATR ~1 -> ext ~5 ATR
    rows = [(ts[i], 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i) for i in range(7)]
    bars = _bars(rows)
    sig = Signal("ORB-15", +1, entry=106.0, target=110.0, stop=104.0, rr=2.0, signal_time=ts[-1])
    eq = compute(sig, bars, _prev_day(200, 50, 106), driver_cluster="A")
    assert eq.veto is True
    assert eq.exec_mult == 0.0
    assert "chase" in eq.reason


def test_no_veto_when_entry_near_recent_price():
    ts = _times(7)
    bars = _bars([(t, 99.9, 100.1, 99.8, 100.0) for t in ts])
    sig = Signal("ORB-15", +1, entry=100.0, target=103.0, stop=99.0, rr=3.0, signal_time=ts[-1])
    eq = compute(sig, bars, _prev_day(200, 50, 100), driver_cluster="A")
    assert eq.veto is False


# ── combination + mq isolation ───────────────────────────────────────────────

def test_exec_mult_is_ms_times_ee_times_cq_mq_excluded():
    ts = _times(7)
    bars = _bars([(t, 99.9, 100.2, 99.7, 100.1) for t in ts])
    sig = Signal("ORB-15", +1, entry=100.1, target=103.0, stop=99.0, rr=2.6, signal_time=ts[-1])
    eq = compute(sig, bars, _prev_day(200, 50, 100), driver_cluster="A")
    assert eq.exec_mult == pytest.approx(round(eq.ms * eq.ee * eq.cq, 3), abs=1e-3)
    assert 0.0 <= eq.mq <= 1.0                              # computed, but not in exec_mult


def test_exec_skip_when_mult_below_floor():
    ts = _times(6)
    # unaccepted PDH just above entry (ms=0.25) + weak candle -> exec_mult < 0.25
    rows = [(t, 99.9, 100.0, 99.0, 99.2) for t in ts]      # long bar closing near low = poor cq
    bars = _bars(rows)
    sig = Signal("ORB-15", +1, entry=100.0, target=106.0, stop=98.0, rr=3.0, signal_time=ts[-1])
    eq = compute(sig, bars, _prev_day(102.0, 97.0, 100.0), driver_cluster="A")
    assert eq.exec_mult < 0.25
    assert eq.skip is True


# ── determinism & early session ──────────────────────────────────────────────

def test_deterministic():
    ts = _times(7)
    bars = _bars([(t, 99.9, 100.2, 99.7, 100.1) for t in ts])
    sig = Signal("ORB-15", +1, entry=100.1, target=103.0, stop=99.0, rr=2.6, signal_time=ts[-1])
    a = compute(sig, bars, _prev_day(200, 50, 100), "A")
    b = compute(sig, bars, _prev_day(200, 50, 100), "A")
    assert (a.ms, a.ee, a.cq, a.exec_mult) == (b.ms, b.ee, b.cq, b.exec_mult)


def test_cq_direction_aware():
    """A hammer (long lower wick, close high) scores well for a long, poorly for a short."""
    ts = _times(5)
    hammer = [(t, 100.0, 100.2, 98.0, 100.1) for t in ts[:-1]]
    hammer.append((ts[-1], 100.0, 100.2, 98.0, 100.1))     # lower wick, close near high
    bars = _bars(hammer)
    long_sig = Signal("PIN-BAR", +1, entry=100.1, target=103, stop=99, rr=2.9, signal_time=ts[-1])
    short_sig = Signal("PIN-BAR", -1, entry=100.1, target=97, stop=101, rr=2.9, signal_time=ts[-1])
    cq_long = compute(long_sig, bars, _prev_day(200, 50, 100), "D").cq
    cq_short = compute(short_sig, bars, _prev_day(200, 50, 100), "D").cq
    assert cq_long > cq_short
