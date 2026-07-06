"""
Regression coverage for the chronological-first driver/stock selection fix
(VALIDATION_PLAN.md — replaces the old whole-day hindsight-max `_pick_driver` /
`best_long = max(long_cands, key=disc_ev)`).

Locks in: a live system deciding at the moment a signal fires cannot know a better
one will show up later, so selection must take the FIRST passing signal in time
order, not the day's best in hindsight — per-stock (_first_chronological_pass) and
across the universe (_earliest).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import backtester.bayesian_engine as be
from strategies.base import Signal


class _StubGate:
    def __init__(self, passed: bool, ev: float):
        self.passed = passed
        self.ev = ev


class _StubExecQ:
    veto = False
    skip = False
    exec_mult = 1.0


def _sig(strategy: str, direction: int, signal_time: str) -> Signal:
    # entry/target/stop just need to make is_valid True
    if direction > 0:
        return Signal(strategy, +1, entry=100.0, target=110.0, stop=95.0,
                      rr=2.0, signal_time=signal_time)
    return Signal(strategy, -1, entry=100.0, target=90.0, stop=105.0,
                  rr=2.0, signal_time=signal_time)


@pytest.fixture
def stub_gates(monkeypatch):
    """gate_map: {strategy_name: (passed, ev)}. Everything passes exec-quality."""
    def _install(gate_map):
        def fake_evaluate_entry(sig, signals, bayes, is_event_day=False, regime="global"):
            passed, ev = gate_map[sig.strategy]
            return _StubGate(passed, ev)
        def fake_exec_compute(sig, today, prev, cl):
            return _StubExecQ()
        monkeypatch.setattr(be, "evaluate_entry", fake_evaluate_entry)
        monkeypatch.setattr(be, "exec_compute", fake_exec_compute)
    return _install


def test_earlier_lower_ev_signal_beats_later_higher_ev_signal_same_stock(stub_gates):
    """ORB-15 (cluster A) fires 09:35 with lower EV; BULL-FLAG (cluster A) fires 11:10
    with higher EV. The old code picked BULL-FLAG (whole-day best). The fix must pick
    ORB-15 because at 09:35 nothing later in the day was knowable yet."""
    signals = {
        "ORB-15":    _sig("ORB-15", +1, "09:35"),
        "BULL-FLAG": _sig("BULL-FLAG", +1, "11:10"),
    }
    stub_gates({"ORB-15": (True, 0.18), "BULL-FLAG": (True, 0.31)})

    ctx = be.DayContext()
    result = be._first_chronological_pass(
        "RELIANCE", signals, bayes=None, direction=+1, ctx=ctx,
        prev=None, today=pd.DataFrame(), get_breadth=lambda hhmm: None)

    assert result is not None
    assert result.driver.strategy == "ORB-15"


def test_failing_earlier_signal_is_skipped_for_next_passing_one(stub_gates):
    """If the earliest signal fails its gate, the scan must continue forward in time
    (not stop dead) and take the next one that actually passes."""
    signals = {
        "ORB-15":    _sig("ORB-15", +1, "09:35"),
        "BULL-FLAG": _sig("BULL-FLAG", +1, "11:10"),
    }
    stub_gates({"ORB-15": (False, 0.0), "BULL-FLAG": (True, 0.31)})

    ctx = be.DayContext()
    result = be._first_chronological_pass(
        "RELIANCE", signals, bayes=None, direction=+1, ctx=ctx,
        prev=None, today=pd.DataFrame(), get_breadth=lambda hhmm: None)

    assert result is not None
    assert result.driver.strategy == "BULL-FLAG"


def test_earliest_picks_first_chronological_passer_across_stocks():
    """Across two stocks' own per-stock winners, the earlier-firing stock's candidate
    wins even though the later-firing stock has a higher disc_ev. Old code
    (`max(cands, key=disc_ev)`) would have picked the later, higher-EV one."""
    early = be.Candidate(symbol="TCS", driver=_sig("ORB-15", +1, "09:35"),
                         gate=None, signals={}, exec_q=None, disc_ev=0.10)
    late = be.Candidate(symbol="INFY", driver=_sig("BULL-FLAG", +1, "13:00"),
                        gate=None, signals={}, exec_q=None, disc_ev=0.90)

    winner = be._earliest([early, late])

    assert winner.symbol == "TCS"


def test_earliest_tie_break_uses_disc_ev_then_symbol():
    """Simultaneous cross-stock signals are legitimately comparable at that instant
    (not lookahead) -- higher disc_ev wins the tie, then symbol name."""
    a = be.Candidate(symbol="ZZZ", driver=_sig("ORB-15", +1, "09:35"),
                     gate=None, signals={}, exec_q=None, disc_ev=0.50)
    b = be.Candidate(symbol="AAA", driver=_sig("BULL-FLAG", +1, "09:35"),
                     gate=None, signals={}, exec_q=None, disc_ev=0.90)

    winner = be._earliest([a, b])

    assert winner.symbol == "AAA"   # higher disc_ev wins the same-instant tie


def test_short_universe_counter_increments_once_per_qualifying_short(monkeypatch):
    """SHORT_UNIVERSE_COUNTER bookkeeping in _process_day is unchanged plumbing around
    the new selection call -- must still increment gated_shorts exactly once per stock
    that produces a qualifying short, and outside_fno only when F&O-ineligible."""
    be.reset_short_universe_counter()

    trade_date = date(2024, 1, 10)
    bars = pd.DataFrame({
        "datetime": pd.date_range(f"{trade_date} 09:15", periods=15, freq="5min"),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000,
    })
    all_data = {"ELIGIBLE": bars.copy(), "INELIGIBLE": bars.copy()}

    def fake_pass(symbol, signals, bayes, direction, ctx, prev, today, get_breadth):
        if direction != -1:
            return None
        return be.Candidate(symbol=symbol, driver=_sig("ORB-15", -1, "09:35"),
                            gate=_StubGate(True, 0.2), signals={},
                            exec_q=_StubExecQ(), disc_ev=0.2)

    monkeypatch.setattr(be, "data_integrity_ok", lambda *a, **k: True)
    monkeypatch.setattr(be, "_first_chronological_pass", fake_pass)
    monkeypatch.setattr(be, "fno_eligible_short",
                        lambda symbol, td: symbol == "ELIGIBLE")
    monkeypatch.setattr(be, "long_eligible", lambda *a, **k: False)
    # synthetic bars have no trailing history, so real ADV would be 0 -- this test is
    # about the F&O counter specifically, not the liquidity gate, so assume liquid
    monkeypatch.setattr(be, "liquidity_eligible", lambda *a, **k: True)
    # counter bookkeeping happens before _build_trade; stub it out so this test
    # doesn't need a real BayesianState/sizer/execution pipeline
    monkeypatch.setattr(be, "_build_trade", lambda *a, **k: None)

    result = be._process_day(trade_date, all_data, None, bayes=None)

    assert be.SHORT_UNIVERSE_COUNTER["gated_shorts"] == 2   # both stocks produced a short
    assert be.SHORT_UNIVERSE_COUNTER["outside_fno"] == 1    # only INELIGIBLE dropped
    assert isinstance(result["recommendations"], list)
