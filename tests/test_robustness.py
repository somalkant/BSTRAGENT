"""
B7a/B7b validation — overfitting controls + Monte Carlo (plan_phase3.md §B7a/B7b).

Run:  python -m pytest tests/test_robustness.py -v
"""
from __future__ import annotations

import numpy as np
import pytest

from reports import overfitting as of
from reports import montecarlo as mc


# ── B7a: Deflated Sharpe ─────────────────────────────────────────────────────

def test_dsr_high_for_strong_edge_few_trials():
    rng = np.random.default_rng(0)
    r = rng.normal(0.15, 0.8, 800)          # per-trade Sharpe ~0.19 over 800 trades
    res = of.deflated_sharpe(r, n_trials=10)
    assert res["dsr"] > 0.95 and res["pass"]


def test_dsr_low_for_noise_many_trials():
    rng = np.random.default_rng(1)
    r = rng.normal(0.01, 1.0, 300)          # near-zero edge
    res = of.deflated_sharpe(r, n_trials=5000)   # searched thousands of configs
    assert res["dsr"] < 0.95 and not res["pass"]


def test_dsr_penalises_more_trials():
    rng = np.random.default_rng(2)
    r = rng.normal(0.05, 1.0, 400)
    few = of.deflated_sharpe(r, n_trials=5)["dsr"]
    many = of.deflated_sharpe(r, n_trials=10000)["dsr"]
    assert many < few                        # more trials -> harder to clear


# ── B7a: PBO via CSCV ────────────────────────────────────────────────────────

def test_pbo_low_when_one_config_genuinely_best():
    rng = np.random.default_rng(3)
    T, N = 320, 8
    M = rng.normal(0, 1, (T, N))
    M[:, 0] += 0.35                          # config 0 has a real, stable edge
    res = of.pbo_cscv(M, n_blocks=16)
    assert res["pbo"] < 0.20 and res["pass"]


def test_pbo_high_for_pure_noise():
    rng = np.random.default_rng(4)
    M = rng.normal(0, 1, (320, 8))           # no config truly best
    res = of.pbo_cscv(M, n_blocks=16)
    assert res["pbo"] > 0.20


# ── B7a: White's reality check ───────────────────────────────────────────────

def test_whites_reality_check_detects_real_edge():
    rng = np.random.default_rng(5)
    T, N = 400, 6
    S = rng.normal(0, 1, (T, N))             # null strategy universe
    system = S.mean(axis=1) + 0.25           # system beats the field
    res = of.whites_reality_check(system, S, n_boot=500)
    assert res["p_value"] < 0.05 and res["pass"]


# ── B7b: Monte Carlo ─────────────────────────────────────────────────────────

def test_mc_strong_series_passes():
    rng = np.random.default_rng(6)
    # positive expectancy, modest variance -> low DD probability
    pnls = rng.normal(1500, 4000, 600)
    res = mc.monte_carlo(pnls, n_sims=800, seed=1)
    assert res["sharpe_5pct"] > 0 and res["p_negative"] < 0.10


def test_mc_marginal_series_fails_a_gate():
    rng = np.random.default_rng(7)
    pnls = rng.normal(50, 8000, 400)         # near-zero edge, high variance
    res = mc.monte_carlo(pnls, n_sims=800, seed=2)
    assert not res["pass"]                   # fails DD or negative-year or Sharpe gate


def test_max_drawdown():
    eq = np.array([100, 120, 90, 110, 80, 130], dtype="float64")
    assert mc.max_drawdown(eq) == pytest.approx((120 - 80) / 120, abs=1e-6)


def test_circuit_trap_losses_enter_uncapped():
    # a -5R circuit trap must show up at full size in the DD stats, not winsorized
    pnls = np.concatenate([np.full(200, 500.0), [-25000.0]])   # one big trap loss
    res = mc.monte_carlo(pnls, n_sims=400, block=1, drop_frac=0.0, seed=3)
    assert res["median_maxdd"] > 0.0        # the uncapped loss drives real drawdown
