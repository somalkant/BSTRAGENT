"""
B2 execution realism + excursion record (plan_phase1.md B2).

Run:  python -m pytest tests/test_execution.py -v
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtester.execution import simulate_execution
from config.integrity import settings_hash
from strategies.base import Signal


def _bars(rows):
    """rows: list of (HH:MM, open, high, low, close). Volume fixed high (no impact slippage)."""
    data = [{"datetime": pd.Timestamp(f"2018-06-01 {t}:00"),
             "open": o, "high": h, "low": lo, "close": c, "volume": 10_000_000}
            for (t, o, h, lo, c) in rows]
    return pd.DataFrame(data)


def test_entry_is_next_bar_open_not_signal_close():
    # signal at 09:15; entry must be the 09:20 open (not the 09:15 close)
    bars = _bars([("09:15", 100, 101, 99, 100.5),
                  ("09:20", 100.6, 101, 100, 100.8),
                  ("09:25", 100.8, 106, 100.5, 105)])
    sig = Signal("X", +1, entry=100.5, target=106, stop=98, rr=2.5, signal_time="09:15")
    r = simulate_execution(sig, bars, shares=1)
    assert r.entry_time == "09:20"
    # slippage is off during training (SLIPPAGE_BPS/IMPACT_K = 0) -- fill is the exact open
    assert r.entry_fill == pytest.approx(100.6, abs=1e-3)


def test_long_target_hit():
    bars = _bars([("09:15", 100, 100, 100, 100),
                  ("09:20", 100, 101, 99.8, 100.5),
                  ("09:25", 100.5, 106.5, 100.4, 106)])   # high 106.5 >= re-anchored target (~106.05)
    sig = Signal("X", +1, entry=100, target=106, stop=98, rr=3.0, signal_time="09:15")
    r = simulate_execution(sig, bars, shares=1)
    assert r.exit_reason == "TARGET"
    # target is re-anchored to entry_fill + the signal's original distance (6), not the raw 106
    reanchored_target = r.entry_fill + (106 - 100)
    # slippage is off during training -- exit fills exactly at the re-anchored target
    assert r.exit_price == pytest.approx(reanchored_target, abs=1e-6)


def test_long_stop_hit():
    bars = _bars([("09:15", 100, 100, 100, 100),
                  ("09:20", 100, 100.2, 97.5, 98)])       # low 97.5 <= stop 98
    sig = Signal("X", +1, entry=100, target=106, stop=98, rr=3.0, signal_time="09:15")
    r = simulate_execution(sig, bars, shares=1)
    assert r.exit_reason == "STOP"


def test_eod_square_off_1510():
    rows = [("09:15", 100, 100, 100, 100), ("09:20", 100, 100.5, 99.5, 100)]
    # drift with no target/stop touch until 15:10
    t = pd.Timestamp("2018-06-01 09:25:00")
    while t.time().strftime("%H:%M") < "15:15":
        rows.append((t.strftime("%H:%M"), 100.0, 100.3, 99.7, 100.0))
        t += pd.Timedelta(minutes=5)
    bars = _bars(rows)
    sig = Signal("X", +1, entry=100, target=110, stop=90, rr=1.0, signal_time="09:15")
    r = simulate_execution(sig, bars, shares=1)
    assert r.exit_reason == "EOD"
    assert r.exit_time == "15:10"


def test_mfe_mae_in_r_units():
    # long entry ~100, risk/share = 2 (fixed R-distance from signal.entry-signal.stop, NOT
    # re-derived from entry_fill). High 104 -> ~+2R favourable, low 99 -> ~-0.5R adverse
    bars = _bars([("09:15", 100, 100, 100, 100),
                  ("09:20", 100, 100, 100, 100),          # entry bar open 100
                  ("09:25", 100, 104, 99, 100),           # max high 104 -> MFE, min low 99 -> MAE
                  ("09:30", 100, 103, 99.9, 100)])         # below 104; target 106 never hit
    sig = Signal("X", +1, entry=100, target=106, stop=98, rr=3.0, signal_time="09:15")
    r = simulate_execution(sig, bars, shares=1)
    entry = r.entry_fill                                   # exactly 100 -- slippage off during training
    stop_dist = 100 - 98                                    # fixed R-distance, preserved through re-anchoring
    assert r.mfe_r == pytest.approx((104 - entry) / stop_dist, abs=0.02)
    assert r.mae_r == pytest.approx((99 - entry) / stop_dist, abs=0.02)
    assert r.mae_r < 0


def test_short_entry_and_target():
    bars = _bars([("09:15", 100, 100, 100, 100),
                  ("09:20", 100, 100.2, 99.8, 100),
                  ("09:25", 100, 100.1, 93.5, 94)])        # low 93.5 <= target 94 (short)
    sig = Signal("X", -1, entry=100, target=94, stop=102, rr=3.0, signal_time="09:15")
    r = simulate_execution(sig, bars, shares=1)
    assert r.exit_reason == "TARGET"
    # slippage is off during training -- short entry fills at the exact open, no discount
    assert r.entry_fill == pytest.approx(100.0, abs=1e-3)


def test_settings_hash_stable():
    h = settings_hash()
    assert isinstance(h, str) and len(h) == 12
    assert settings_hash() == h            # deterministic
