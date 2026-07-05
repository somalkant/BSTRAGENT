"""
B5 validation — loss-threshold halt & approval gate (plan_phase3.md §B5).

Run:  python -m pytest tests/test_loss_halt.py -v
"""
from __future__ import annotations

import pandas as pd
import pytest

from reports import loss_halt as lh
from config.settings import CAPITAL, DAILY_LOSS_HALT, MONTHLY_LOSS_HALT


def _df(rows):
    return pd.DataFrame(rows, columns=["date", "pnl_rs", "actual_risk"])


def test_daily_halt_triggers(tmp_path):
    p = tmp_path / "halt.json"
    # one day breaching the daily halt (0.96% of 10L = 9600)
    df = _df([("2020-03-12", -6000, 5000), ("2020-03-12", -6000, 5000)])
    st = lh.check_loss_halt(df, CAPITAL, p)
    assert st["halted"] and st["kind"] == "daily"


def test_no_halt_on_normal_day(tmp_path):
    p = tmp_path / "halt.json"
    df = _df([("2020-06-01", 3000, 5000), ("2020-06-01", -2000, 5000)])
    st = lh.check_loss_halt(df, CAPITAL, p)
    assert not st["halted"]


def test_monthly_halt_triggers(tmp_path):
    p = tmp_path / "halt.json"
    # spread small daily losses across a month summing < -4% (40000)
    rows = [(f"2022-06-{d:02d}", -3000, 5000) for d in range(1, 16)]   # -45000
    st = lh.check_loss_halt(_df(rows), CAPITAL, p)
    assert st["halted"] and st["kind"] == "monthly"


def test_halt_state_survives_restart(tmp_path):
    p = tmp_path / "halt.json"
    df = _df([("2020-03-12", -10000, 5000)])
    lh.check_loss_halt(df, CAPITAL, p)
    assert lh.is_halted(p) is True                    # reloaded from disk
    # a later benign trade does NOT clear the halt without approval
    lh.check_loss_halt(_df([("2020-03-13", 1000, 5000)]), CAPITAL, p)
    assert lh.is_halted(p) is True


def test_approval_resumes(tmp_path):
    p = tmp_path / "halt.json"
    lh.check_loss_halt(_df([("2020-03-12", -10000, 5000)]), CAPITAL, p)
    st = lh.approve_resume(p, when=None)
    assert st["halted"] is False and st["approved_at"] is not None
    assert lh.is_halted(p) is False


def test_advisories_consecutive_losses(tmp_path):
    p = tmp_path / "halt.json"
    df = _df([("2020-06-01", -1000, 5000), ("2020-06-01", -1000, 5000),
              ("2020-06-01", -1000, 5000)])            # 3 consecutive losers
    st = lh.check_loss_halt(df, CAPITAL, p)
    assert any("consecutive_losses" in a for a in st.get("advisories", []))


def test_alarms_surfaced_in_report(tmp_path):
    p = tmp_path / "halt.json"
    df = _df([("2020-06-01", 1000, 5000)])
    st = lh.check_loss_halt(df, CAPITAL, p, alarms=["CHANGEPOINT ORB-15"])
    assert any("alarm:" in a for a in st.get("advisories", []))
