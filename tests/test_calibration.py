"""
B5 validation — calibration & drift reporting (plan_phase3.md §B5).

Run:  python -m pytest tests/test_calibration.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from reports import calibration as cal


def _trades(pwins, wins, risk=5000.0, ev=0.5, mfe=0.5, exit_reason="STOP",
            shash="abc123", time_bucket="T2", breadth=0.5):
    """Build a trades DataFrame; wins is a list of 0/1, pwins the predicted P(win)."""
    n = len(pwins)
    return pd.DataFrame({
        "driver_p": pwins,
        "pnl_rs": [risk if w else -risk for w in wins],
        "actual_risk": [risk] * n,
        "ev": [ev] * n,
        "mfe_r": [mfe] * n,
        "exit_reason": [exit_reason] * n,
        "settings_hash": [shash] * n,
        "time_bucket": [time_bucket] * n,
        "breadth": [breadth] * n,
        "driver_strategy": ["ORB-15"] * n,
    })


def test_ece_near_zero_for_calibrated_stream():
    rng = np.random.default_rng(0)
    pwins, wins = [], []
    for p in (0.45, 0.52, 0.57, 0.62, 0.70):
        for _ in range(200):
            pwins.append(p)
            wins.append(1 if rng.random() < p else 0)
    r = cal.ece(_trades(pwins, wins))
    assert r["ece"] < 0.05 and r["pass"]


def test_ece_high_for_overconfident_stream():
    # predicts 0.70 but only wins 40%
    pwins = [0.70] * 200
    wins = [1] * 80 + [0] * 120
    r = cal.ece(_trades(pwins, wins))
    assert r["ece"] > 0.25 and not r["pass"]


def test_brier_bounds():
    # high-conviction correct predictions clear 0.22; over-confident-wrong is far worse
    good = cal.brier(_trades([0.75] * 100, [1] * 75 + [0] * 25))
    bad = cal.brier(_trades([0.9] * 100, [1] * 40 + [0] * 60))
    assert good["brier"] < 0.22 and good["pass"]
    assert bad["brier"] > good["brier"]


def test_ev_realisation_flags_overconfidence():
    # EV predicted 0.6 but trades realise near 0 -> ratio well below 0.5
    df = _trades([0.6] * 100, [1] * 45 + [0] * 55, ev=0.6)
    r = cal.ev_realisation(df)
    assert r["ratio"] < 0.5 and not r["pass"]


def test_prob_drift_fires_on_15pt_drift():
    # first 40 well-calibrated at 0.6; then 40 at predicted 0.63 but realised 0.40
    pwins = [0.6] * 40 + [0.63] * 40
    wins = ([1] * 24 + [0] * 16) + ([1] * 16 + [0] * 24)
    alarms = cal.prob_drift(_trades(pwins, wins))
    assert len(alarms) > 0
    assert max(a["gap"] for a in alarms) > 0.15


def test_prob_drift_silent_on_calibrated():
    # deterministic 60% stream (period-5 pattern) -> every 30-window is exactly 0.60
    pwins = [0.6] * 120
    wins = ([1, 1, 1, 0, 0] * 24)
    assert len(cal.prob_drift(_trades(pwins, wins))) == 0


def test_loss_decomposition_classes():
    # 3 losers: never-worked (mfe 0.1), gave-back (mfe 1.5), truncated (EOD, mfe 0.5)
    df = pd.DataFrame({
        "pnl_rs": [-5000, -5000, -5000, 5000],
        "actual_risk": [5000] * 4,
        "mfe_r": [0.1, 1.5, 0.5, 2.0],
        "exit_reason": ["STOP", "STOP", "EOD", "TARGET"],
    })
    d = cal.loss_decomposition(df)
    assert d["never_worked"] == 1 and d["gave_back"] == 1 and d["truncated"] == 1
    assert d["winners"] == 1 and d["capture_ratio"] is not None


def test_config_integrity_drift():
    clean = cal.config_integrity(_trades([0.6] * 10, [1] * 10, shash="hashA"))
    assert clean["present_pct"] == 100.0 and not clean["config_drift"]
    mixed = pd.concat([_trades([0.6] * 5, [1] * 5, shash="hashA"),
                       _trades([0.6] * 5, [1] * 5, shash="hashB")], ignore_index=True)
    assert cal.config_integrity(mixed)["config_drift"] is True


def test_strategy_importance_flags_established_negative():
    df = _trades([0.5] * 50, [1] * 20 + [0] * 30)     # net negative realised R
    imp = cal.strategy_importance(df, neff={"ORB-15": 60})   # established
    assert bool(imp.iloc[0]["flag"]) is True


def test_generate_report_bundle():
    rep = cal.generate_report(_trades([0.6] * 50, [1] * 30 + [0] * 20))
    assert rep["n_trades"] == 50
    assert "ece" in rep and "brier" in rep and "loss_decomposition" in rep
