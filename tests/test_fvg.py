"""
FVG (Fair Value Gap) — retrace-into-gap-and-resume, not immediate chase.

Locks in the fix: the strategy's own docstring says it enters "when price retraces
INTO the gap zone and resumes," but the original implementation entered immediately
on gap detection with no retrace check at all (chasing the print). These tests prove
the corrected behavior.

Run:  python -m pytest tests/test_fvg.py -v
"""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.structure.fvg import FairValueGap


def _bars(rows):
    """rows: list of (HH:MM, open, high, low, close, volume)."""
    data = [{"datetime": pd.Timestamp(f"2018-06-01 {t}:00"),
             "open": o, "high": h, "low": lo, "close": c, "volume": v}
            for (t, o, h, lo, c, v) in rows]
    return pd.DataFrame(data)


def _no_history():
    return pd.DataFrame({"datetime": [], "open": [], "high": [], "low": [], "close": [], "volume": []})


def test_no_entry_on_immediate_gap_no_retrace():
    """Bullish gap forms (bar3.low > bar1.high) and price just keeps running away --
    never comes back to fill/test the gap. Must NOT enter (old code chased this)."""
    bars = _bars([
        ("09:15", 100, 101, 99, 100.5, 10000),
        ("09:20", 100.6, 101, 100.5, 100.8, 10000),   # bar1: high=101
        ("09:25", 102, 103, 101.5, 102.5, 10000),     # bar3: low=101.5 > bar1.high=101 -> gap [101,101.5]
        ("09:30", 103, 104, 102.8, 103.5, 10000),     # keeps running, never retraces into gap
        ("09:35", 104, 105, 103.8, 104.5, 10000),
    ])
    strat = FairValueGap()
    sig = strat.generate_signal(bars, _no_history(), None, bars, pd.Timestamp("2018-06-01").date())
    assert sig.direction == 0


def test_entry_on_retrace_into_gap_and_resume():
    """Same bullish gap, but a later bar dips back into the gap zone and closes back
    above it (resume) -- this is exactly the pattern the docstring describes."""
    bars = _bars([
        ("09:15", 100, 101, 99, 100.5, 10000),
        ("09:20", 100.6, 101, 100.5, 100.8, 10000),   # bar1: high=101
        ("09:25", 102, 103, 101.5, 102.5, 10000),     # bar3: low=101.5 -> gap [101, 101.5]
        ("09:30", 102.4, 102.6, 101.2, 101.4, 10000), # retraces into [101,101.5], closes above 101
        ("09:35", 101.6, 102, 101.4, 101.9, 10000),
    ])
    strat = FairValueGap()
    sig = strat.generate_signal(bars, _no_history(), None, bars, pd.Timestamp("2018-06-01").date())
    assert sig.direction == +1
    assert sig.signal_time == "09:30"
    assert sig.stop == pytest.approx(101.0)          # gap_lo
    assert sig.entry == pytest.approx(101.4)         # that bar's close


def test_gap_invalidated_if_price_breaks_fully_through():
    """If price later trades clean through the whole gap zone without holding (high <
    gap_lo for a bullish gap means it broke back down through it), the gap is dead --
    must not treat a later, unrelated bounce as if it were still testing that gap."""
    bars = _bars([
        ("09:15", 100, 101, 99, 100.5, 10000),
        ("09:20", 100.6, 101, 100.5, 100.8, 10000),   # bar1: high=101
        ("09:25", 102, 103, 101.5, 102.5, 10000),     # bar3: low=101.5 -> gap [101, 101.5]
        ("09:30", 100.5, 100.8, 100.0, 100.2, 10000), # high=100.8 < gap_lo=101 -> broke clean through, invalidated
        ("09:35", 101.2, 101.4, 101.05, 101.3, 10000),# a later bounce back into the old zone must NOT trigger
    ])
    strat = FairValueGap()
    sig = strat.generate_signal(bars, _no_history(), None, bars, pd.Timestamp("2018-06-01").date())
    assert sig.direction == 0
