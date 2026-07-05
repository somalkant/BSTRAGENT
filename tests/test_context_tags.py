"""
B4f validation — context tags + the single breadth sizing rule + signal label
(plan_phase2.md §B4f, exit criteria).

Run:  python -m pytest tests/test_context_tags.py -v
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtester.context_tags import compute_tags, context_mult, signal_label, time_bucket
from config.settings import CONTEXT_MULT_OPPOSED
from strategies.base import Signal


def _bars(rows):
    return pd.DataFrame([
        {"datetime": pd.Timestamp(f"2018-06-01 {t}:00"),
         "open": o, "high": h, "low": lo, "close": c, "volume": 100000}
        for (t, o, h, lo, c) in rows])


def _prev(high, low, close):
    return pd.Series({"high": high, "low": low, "close": close, "open": close, "volume": 1})


def test_time_buckets():
    assert time_bucket("09:20") == "T1"
    assert time_bucket("11:00") == "T2"
    assert time_bucket("14:00") == "T3"


def test_day_type_and_gap():
    bars = _bars([("09:15", 103, 104, 102, 103), ("09:20", 103, 105, 102.5, 104)])
    tags = compute_tags(Signal("X", +1, 103, 106, 101, 2, signal_time="09:15"),
                        bars, _prev(104, 101, 100), history_5min=pd.DataFrame())
    assert tags.gap_pct == pytest.approx(3.0, abs=0.1)     # open 103 vs prev close 100
    assert tags.time_bucket == "T1"
    assert tags.day_type in ("inside", "outside", "normal")


def test_inside_day():
    bars = _bars([("09:15", 100, 101, 99.5, 100.5)])       # within prev day's 98-102
    tags = compute_tags(Signal("X", +1, 100, 103, 99, 3, signal_time="09:15"),
                        bars, _prev(102, 98, 100), history_5min=pd.DataFrame())
    assert tags.day_type == "inside"


def test_daily_trend_from_history():
    # 25 rising daily closes -> last close above EMA20 -> "up"
    days = pd.date_range("2018-04-01 15:25", periods=25, freq="D")
    hist = pd.DataFrame({"datetime": days, "close": range(100, 125),
                         "open": range(100, 125), "high": range(100, 125),
                         "low": range(100, 125), "volume": [1] * 25})
    tags = compute_tags(Signal("X", +1, 130, 133, 129, 3, signal_time="09:15"),
                        _bars([("09:15", 130, 131, 129, 130)]), _prev(131, 129, 130), hist)
    assert tags.daily_trend == "up"


# ── the ONE sizing rule: breadth opposition ──────────────────────────────────

def test_context_mult_breadth_opposition():
    assert context_mult(+1, breadth=0.20) == CONTEXT_MULT_OPPOSED   # long into weak breadth
    assert context_mult(-1, breadth=0.80) == CONTEXT_MULT_OPPOSED   # short into strong breadth
    assert context_mult(+1, breadth=0.55) == 1.0
    assert context_mult(-1, breadth=0.40) == 1.0
    assert context_mult(+1, breadth=None) == 1.0                    # unknown -> neutral


# ── signal-level outcome label (log-only) ────────────────────────────────────

def test_signal_label_favourable():
    ts = [f"09:{m:02d}" for m in (15, 20, 25, 30)]
    # long entry 100, ATR 1 -> +0.5 move = 100.5; a later bar hits 100.6 first
    bars = _bars([(ts[0], 100, 100.1, 99.9, 100), (ts[1], 100, 100.2, 99.9, 100.1),
                  (ts[2], 100.1, 100.6, 100.0, 100.5), (ts[3], 100.5, 100.7, 100.3, 100.6)])
    sig = Signal("X", +1, entry=100.0, target=103, stop=99, rr=3, signal_time=ts[0])
    assert signal_label(sig, bars, atr=1.0) == 1.0


def test_signal_label_adverse():
    ts = [f"09:{m:02d}" for m in (15, 20, 25)]
    bars = _bars([(ts[0], 100, 100.1, 99.9, 100), (ts[1], 100, 100.1, 99.3, 99.4),
                  (ts[2], 99.4, 99.5, 99.2, 99.3)])
    sig = Signal("X", +1, entry=100.0, target=103, stop=99, rr=3, signal_time=ts[0])
    assert signal_label(sig, bars, atr=1.0) == 0.0        # dropped 0.5 ATR first


def test_signal_label_neither():
    ts = [f"09:{m:02d}" for m in (15, 20, 25)]
    bars = _bars([(ts[0], 100, 100.1, 99.9, 100), (ts[1], 100, 100.1, 99.95, 100),
                  (ts[2], 100, 100.05, 99.95, 100)])
    sig = Signal("X", +1, entry=100.0, target=103, stop=99, rr=3, signal_time=ts[0])
    assert signal_label(sig, bars, atr=1.0) == 0.5
