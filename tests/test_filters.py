"""
B2 time & macro filters (plan_phase1.md B2).

Run:  python -m pytest tests/test_filters.py -v
"""
from __future__ import annotations

from datetime import date, time

from backtester.filters import (
    first_candle_filter, after_last_entry, event_day, event_mode,
)
from strategies.base import Signal


def _sig(name, d=+1):
    return Signal(name, d, entry=100, target=106, stop=98, rr=3.0)


def test_first_candle_blocks_non_exempt_at_0915():
    sigs = {"EMA-CROSS": _sig("EMA-CROSS"), "GAP-CONT": _sig("GAP-CONT"),
            "BOLLINGER": _sig("BOLLINGER"), "CPR": _sig("CPR")}
    kept = first_candle_filter(sigs, "09:15")
    assert set(kept) == {"GAP-CONT", "CPR"}     # only FIRST_CANDLE_EXEMPT survive


def test_first_candle_passthrough_other_bars():
    sigs = {"EMA-CROSS": _sig("EMA-CROSS")}
    assert first_candle_filter(sigs, "09:20") == sigs
    assert first_candle_filter(sigs, time(9, 20)) == sigs


def test_last_entry_cutoff():
    assert after_last_entry("14:30") is True
    assert after_last_entry("14:35") is True
    assert after_last_entry("14:25") is False
    assert after_last_entry(time(9, 30)) is False


def test_union_budget_is_event_day():
    is_ev, events = event_day(date(2018, 2, 1))
    assert is_ev and "UNION_BUDGET" in events
    assert event_day(date(2018, 6, 15)) == (False, [])


def test_event_mode_default_skip():
    assert event_mode() == "SKIP"
