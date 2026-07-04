"""
B2 gate validation — cluster gate + EV/driver soft gates (plan_phase1.md §2g).

Run:  python -m pytest tests/test_bayesian_gate.py -v
"""
from __future__ import annotations

import numpy as np
import pytest

import backtester.bayesian_gate as bg
from backtester.bayesian_gate import count_clusters, evaluate_entry
from weights.bayesian import BayesianState
from strategies.base import Signal

# one representative strategy per cluster (from config/strategy_clusters.json)
A, B, C, D, E = "ORB-15", "BOLLINGER", "EMA-CROSS", "PIN-BAR", "CPR"


def _bayes(**mu_by_key) -> BayesianState:
    """key 'ORB-15:long' -> mu, with huge n_eff so p_win ≈ mu (shrink negligible)."""
    st = BayesianState()
    for key, mu in mu_by_key.items():
        name, d = key.rsplit(":", 1)
        cell = st._cell(name, d)
        total = 200.0
        cell.alpha, cell.beta, cell.n_eff = mu * total, (1 - mu) * total, 1e6
    return st


def _sig(strategy, direction, rr=1.85):
    e, s = 100.0, 99.0
    t = e + rr * (e - s) if direction > 0 else e - rr * (e - s)
    if direction < 0:
        s = e + 1.0
        t = e - rr * (s - e)
    return Signal(strategy=strategy, direction=direction, entry=e, target=t, stop=s, rr=rr)


# ── cluster counting + eff (§2g plan examples) ───────────────────────────────

def test_plan_eff_examples():
    st = _bayes(**{f"{B}:short": 0.65, f"{D}:short": 0.65})
    sigs = {B: _sig(B, -1), D: _sig(D, -1)}
    cr = count_clusters(sigs, -1, st)
    assert cr.confirmed == {"B", "D"}
    assert cr.eff_binary == pytest.approx(1.739, abs=0.01)   # B,D corr 0.15 -> PASS

    st2 = _bayes(**{f"{A}:long": 0.65, f"{C}:long": 0.65})
    cr2 = count_clusters({A: _sig(A, 1), C: _sig(C, 1)}, 1, st2)
    assert cr2.confirmed == {"A", "C"}
    assert cr2.eff_binary == pytest.approx(1.25, abs=0.01)    # A,C corr 0.6 -> REJECT


def test_all_floor_confidence_makes_weighted_equal_binary():
    """When every confirming cluster sits at the c-floor, eff_weighted == eff_binary."""
    st = _bayes(**{f"{B}:short": 0.50, f"{D}:short": 0.50})   # p_win 0.5 -> c floored to 0.3
    cr = count_clusters({B: _sig(B, -1), D: _sig(D, -1)}, -1, st)
    assert set(cr.confidence.values()) == {0.3}
    assert cr.eff_weighted == pytest.approx(cr.eff_binary, abs=1e-6)


def test_context_cluster_vote_fixed_at_one():
    """Cluster E votes at c=1.0 regardless of its (never-updated) posterior."""
    st = _bayes(**{f"{B}:short": 0.60, f"{E}:short": 0.50})
    cr = count_clusters({B: _sig(B, -1), E: _sig(E, -1)}, -1, st)
    assert cr.confidence["E"] == 1.0


# ── the four rejection reasons (exit criterion: all appear) ──────────────────

def test_reject_single_cluster():
    st = _bayes(**{f"{B}:short": 0.65})
    r = evaluate_entry(_sig(B, -1), {B: _sig(B, -1)}, st)
    assert not r.passed and r.reason == "single-cluster"


def test_reject_low_effective_clusters():
    st = _bayes(**{f"{A}:long": 0.65, f"{C}:long": 0.65})
    r = evaluate_entry(_sig(A, 1), {A: _sig(A, 1), C: _sig(C, 1)}, st)
    assert not r.passed and r.reason == "low-effective-clusters"


def test_reject_weak_driver():
    # 3 confirming clusters (E votes c=1.0) keep eff >= 1.5 so the driver gate is isolated
    st = _bayes(**{f"{B}:short": 0.51, f"{D}:short": 0.65})
    sigs = {B: _sig(B, -1), D: _sig(D, -1), E: _sig(E, -1)}
    r = evaluate_entry(_sig(B, -1), sigs, st)
    assert not r.passed and r.reason == "weak-driver"


def test_reject_low_ev():
    # rr=1.0, driver p≈0.55 -> passes driver gate (>=0.52) but EV=0.10 < 0.15
    st = _bayes(**{f"{B}:short": 0.55, f"{D}:short": 0.65})
    sigs = {B: _sig(B, -1, rr=1.0), D: _sig(D, -1), E: _sig(E, -1)}
    r = evaluate_entry(_sig(B, -1, rr=1.0), sigs, st)
    assert not r.passed and r.reason == "low-EV"


def test_reject_equal_opposition_logs_cf_contra():
    """confirmed=2, contradicting=2 -> reject; weak opposition -> CF_CONTRA True."""
    st = _bayes(**{f"{B}:short": 0.65, f"{D}:short": 0.65,
                   f"{A}:long": 0.50, f"{C}:long": 0.50})
    sigs = {B: _sig(B, -1), D: _sig(D, -1), A: _sig(A, 1), C: _sig(C, 1)}
    r = evaluate_entry(_sig(B, -1), sigs, st)
    assert not r.passed and r.reason == "equal-opposition"
    assert r.cf_contra is True          # two floor-confidence oppositions sum <= 1.0


def test_breakout_driver_blocked_by_trend():
    """Driver cluster A + cluster C contradicting -> reject regardless of other gates."""
    st = _bayes(**{f"{A}:long": 0.65, f"{D}:long": 0.65, f"{C}:short": 0.65})
    sigs = {A: _sig(A, 1), D: _sig(D, 1), C: _sig(C, -1)}
    r = evaluate_entry(_sig(A, 1), sigs, st)
    assert not r.passed and r.reason == "breakout-blocked-by-trend"


def test_reversion_driver_tolerates_one_contradiction():
    """Driver cluster B + one contradicting cluster C -> normal tolerance (passes)."""
    st = _bayes(**{f"{B}:short": 0.65, f"{D}:short": 0.65, f"{C}:long": 0.60})
    sigs = {B: _sig(B, -1), D: _sig(D, -1), C: _sig(C, 1)}
    r = evaluate_entry(_sig(B, -1), sigs, st)
    assert r.passed and r.clusters.contradicting == {"C"}


# ── clean pass + soft-gate ramp ──────────────────────────────────────────────

def test_clean_pass_full_size():
    st = _bayes(**{f"{B}:short": 0.65, f"{D}:short": 0.65})
    r = evaluate_entry(_sig(B, -1), {B: _sig(B, -1), D: _sig(D, -1)}, st)
    assert r.passed and not r.clusters.contradicting
    assert r.gate_mult == pytest.approx(1.0)          # EV & driver both past full-size anchors
    assert "CONFIRMED-CLEAN" in r.log_line()


def test_soft_gate_token_size_near_ev_floor():
    """Near the EV floor a passing trade sizes at gate_mult < 0.2 (ramp working)."""
    # driver p=0.53, rr=1.19 -> EV≈0.16 (ev_mult≈0.1); driver_mult≈0.17; E keeps eff up
    st = _bayes(**{f"{B}:short": 0.53, f"{D}:short": 0.65})
    sigs = {B: _sig(B, -1, rr=1.19), D: _sig(D, -1), E: _sig(E, -1)}
    r = evaluate_entry(_sig(B, -1, rr=1.19), sigs, st)
    assert r.passed
    assert 0.0 < r.gate_mult < 0.2          # exit criterion: near EV=0.16 -> gate_mult < 0.2


# ── dual gate non-monotonicity (§2g "Why the gate is dual") ───────────────────

def test_eff_non_monotone_downweighting_can_raise_eff(monkeypatch):
    """Plan example: corr(1,2)=0.9 + independent 3rd. Lowering c1 RAISES eff."""
    C = np.array([[1, 0.9, 0], [0.9, 1, 0], [0, 0, 1]], float)
    monkeypatch.setattr(bg, "_C", C)
    monkeypatch.setattr(bg, "_CLUSTER_ORDER", ["X", "Y", "Z"])
    monkeypatch.setattr(bg, "_CIDX", {"X": 0, "Y": 1, "Z": 2})
    eff_binary = bg._eff({"X": 1, "Y": 1, "Z": 1})
    eff_down = bg._eff({"X": 0.9, "Y": 1, "Z": 1})
    assert eff_binary == pytest.approx(1.875, abs=0.001)
    assert eff_down == pytest.approx(1.899, abs=0.001)
    assert eff_down > eff_binary            # perverse: downweighting raises eff


def test_dual_gate_binary_floor_blocks_weighted_rescue():
    """eff_binary < 1.5 rejects even though weighting alone might lift eff_weighted."""
    st = _bayes(**{f"{A}:long": 0.65, f"{C}:long": 0.65})   # A,C -> eff_binary 1.25
    cr = count_clusters({A: _sig(A, 1), C: _sig(C, 1)}, 1, st)
    assert cr.eff_binary < 1.5
    r = evaluate_entry(_sig(A, 1), {A: _sig(A, 1), C: _sig(C, 1)}, st)
    assert not r.passed          # dual AND gate: binary floor wins
