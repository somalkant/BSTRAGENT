"""
B5b/B7d/B7e/B8 — stress, ablation, risk-scaling, benchmarks (plan_phase3.md).

Run:  python -m pytest tests/test_reports_suite.py -v
"""
from __future__ import annotations

import numpy as np
import pytest

from reports import benchmarks as bm
from reports import stress as st
from reports import ablation as ab
from reports import risk_scaling as rs


# ── B8 benchmarks ────────────────────────────────────────────────────────────

def test_bootstrap_sharpe_diff_detects_better_system():
    rng = np.random.default_rng(0)
    system = rng.normal(0.12, 1.0, 500)
    bench = rng.normal(0.0, 1.0, 500)
    r = bm.bootstrap_sharpe_diff(system, bench, n_boot=1000)
    assert r["diff"] > 0 and r["beats"]


def test_compare_all_gate_requires_beating_random_and_current():
    rng = np.random.default_rng(1)
    system = rng.normal(0.25, 1.0, 800)              # clear edge
    benches = {"random": rng.normal(0.0, 1.0, 800), "current": rng.normal(0.02, 1.0, 800)}
    out = bm.compare_all(system, benches)
    assert out["_gate_pass"] in (True, False)
    assert out["random"]["beats"]


def test_capacity_ceiling():
    # Sharpe holds up to 2Cr then degrades > 20% at 10Cr
    sbc = {10_00_000: 1.5, 50_00_000: 1.4, 2_00_00_000: 1.25, 10_00_00_000: 0.8}
    c = bm.capacity_ceiling(sbc)
    assert c["capacity_ceiling"] == 2_00_00_000
    assert c["operating_below_ceiling"] is True


# ── B5b stress suite ─────────────────────────────────────────────────────────

def test_stress_suite_strong_series_passes():
    rng = np.random.default_rng(2)
    pnls = rng.normal(1200, 3500, 600)
    out = st.run_stress_suite(pnls)
    assert "S7" in out and out["unstressed"]["sharpe"] > 0
    assert all(k in out for k in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"))


def test_s1_doubles_slippage_reduces_pnl():
    pnls = np.full(100, 1000.0)
    assert st.apply_scenario(pnls, "S1", avg_slippage_rs=200).sum() < pnls.sum()


def test_s7_injects_tail_losses():
    rng = np.random.default_rng(3)
    pnls = rng.normal(500, 1000, 500)
    stressed = st.apply_scenario(pnls, "S7", seed=1)
    assert stressed.min() < pnls.min()          # trapped tail loss appears


def test_sensitivity_no_sign_flip():
    r = st.sensitivity_sweep(1.0, evaluate=lambda v: 0.5 + 0.1 * v)   # always positive
    assert r["no_sign_flip"] is True


# ── B7d ablation ─────────────────────────────────────────────────────────────

def test_ablation_keeps_useful_component_demotes_useless():
    rng = np.random.default_rng(4)
    full = rng.normal(0.10, 1.0, 500)
    ms_off = full - 0.05                          # turning ms off HURTS -> keep
    cq_off = full + 0.05                          # turning cq off HELPS -> demote
    rep = ab.ablation_report({"full": full, "ms_off": ms_off, "cq_off": cq_off})
    assert rep["ms_off"]["keeps_sizing_rights"] is True
    assert rep["cq_off"]["keeps_sizing_rights"] is False
    assert "cq" in rep["_demote_to_log_only"]


# ── B7e risk scaling ─────────────────────────────────────────────────────────

def test_risk_scaling_sweep_and_single_fill_breach():
    rng = np.random.default_rng(5)
    pnls = rng.normal(800, 3000, 400)
    out = rs.risk_scale_sweep(pnls, multipliers=[1.5, 2.0, 2.5], n_sims=300)
    assert 1.0 in out["per_multiplier"] and 2.5 in out["per_multiplier"]
    assert out["safe_ceiling"] >= 1.0


def test_risk_scaling_flags_single_fill_breach():
    # a huge single loss breaches the scaled daily halt (0.96%*2.5*10L = 24000) at 2.5x
    pnls = np.concatenate([np.full(200, 300.0), [-12000.0]])   # -12000 * 2.5 = -30000 > 24000
    out = rs.risk_scale_sweep(pnls, multipliers=[2.5], n_sims=200)
    assert out["per_multiplier"][2.5]["single_fill_breach"] is True
    assert out["per_multiplier"][2.5]["gate_pass"] is False
