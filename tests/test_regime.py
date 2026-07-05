"""
B3 validation — regime classifier + regime-conditional posteriors + blending
(plan_phase2.md §1, exit criteria).

Run:  python -m pytest tests/test_regime.py -v
"""
from __future__ import annotations

import numpy as np
import pytest

from weights.regime import vix_thresholds, raw_regime, RegimeClassifier
from weights.bayesian import BayesianState
from config.settings import REGIME_MIN_NEFF, HYSTERESIS_DAYS


# ── rolling-percentile threshold ─────────────────────────────────────────────

def test_vix_thresholds_none_when_short_history():
    assert vix_thresholds([12, 13, 14]) is None


def test_vix_thresholds_ordered_percentiles():
    rng = np.random.default_rng(0)
    closes = list(10 + 5 * rng.random(252))
    t = vix_thresholds(closes)
    assert t["p_lo"] <= t["p80"] <= t["p_hi"]


def test_vix_threshold_is_relative_not_fixed():
    """A low-VIX regime year: P80 sits near ~12, so VIX 13 is locally R1 (a fixed
    VIX>20 would never fire) — the whole point of the percentile threshold."""
    closes = list(np.linspace(9, 13, 252))       # 2017-like low-vol year
    t = vix_thresholds(closes)
    assert t["p80"] < 20
    assert raw_regime(vix=t["p80"] + 0.5, adx=15, nifty_ret=0.2, vix_p80=t["p80"]) == "R1"


# ── raw regime logic ─────────────────────────────────────────────────────────

def test_raw_regime_states():
    assert raw_regime(30, 40, -3.0, 20) == "R4"    # crash dominates
    assert raw_regime(25, 40, 0.5, 20) == "R1"     # high vix
    assert raw_regime(15, 30, 0.5, 20) == "R2"     # trending (adx>25)
    assert raw_regime(15, 18, 0.5, 20) == "R3"     # sideways
    assert raw_regime(15, 18, 0.5, None) == "R3"   # no threshold -> never R1


# ── hysteresis buffer ────────────────────────────────────────────────────────

def test_hysteresis_requires_consecutive_days():
    rc = RegimeClassifier(active="R3")
    # one R2 day: still R3 (pending)
    assert rc.classify(15, 30, 0.1, 20) == "R3"
    assert rc.classify(15, 30, 0.1, 20) == "R3"
    # third consecutive R2 day commits
    assert rc.classify(15, 30, 0.1, 20) == "R2"


def test_hysteresis_resets_on_flip():
    rc = RegimeClassifier(active="R3")
    rc.classify(15, 30, 0.1, 20)          # R2 pending 1
    rc.classify(25, 15, 0.1, 20)          # R1 pending 1 (flip resets R2)
    assert rc.active == "R3"
    # need 3 consecutive R1 now
    rc.classify(25, 15, 0.1, 20)
    assert rc.classify(25, 15, 0.1, 20) == "R1"


def test_r4_is_immediate():
    rc = RegimeClassifier(active="R2")
    assert rc.classify(15, 30, -2.5, 20) == "R4"    # no buffer
    # leaving R4 is buffered like any R1/R2/R3 transition
    assert rc.classify(15, 18, 0.1, 20) == "R4"


# ── regime-conditional posteriors (fallback) ─────────────────────────────────

def _fill(bayes, strat, d, regime, n, score_pnl):
    for _ in range(n):
        bayes.update(strat, d, pnl_rs=score_pnl, risk_amount=5000, rr=1.85, regime=regime)


def test_regime_fallback_below_min_neff():
    b = BayesianState()
    # 10 R1 trades (< 20) -> get_posterior('R1') falls back to global
    _fill(b, "ORB-15", "long", "R1", 10, 1.85 * 5000)
    assert b.get_posterior("ORB-15", "long", "R1").n_eff == b.get_posterior("ORB-15", "long").n_eff


def test_regime_used_above_min_neff():
    b = BayesianState()
    # R1 wins (high score), global will also include some R3 losses
    _fill(b, "ORB-15", "long", "R1", REGIME_MIN_NEFF + 5, 1.85 * 5000)   # all wins in R1
    _fill(b, "ORB-15", "long", "R3", 30, -5000)                          # losses in R3
    p_r1 = b.get_posterior("ORB-15", "long", "R1")
    p_global = b.get_posterior("ORB-15", "long")
    assert p_r1.n_eff >= REGIME_MIN_NEFF
    assert p_r1.mu > p_global.mu       # R1 posterior is cleaner than the mixed global


def test_global_always_updated_alongside_regime():
    b = BayesianState()
    _fill(b, "X", "long", "R2", 5, 1.85 * 5000)
    assert b.get_posterior("X", "long").n_eff == pytest.approx(5, abs=0.01)   # global got all 5


# ── boundary blending ────────────────────────────────────────────────────────

def _set(b, strat, d, regime, mu, n_eff):
    cell = b._cell(strat, d, regime)
    total = 60.0
    cell.alpha, cell.beta, cell.n_eff = mu * total, (1 - mu) * total, n_eff


def test_blend_monotone_across_vix_band():
    b = BayesianState()
    _set(b, "S", "long", "R1", mu=0.70, n_eff=50)
    _set(b, "S", "long", "R3", mu=0.40, n_eff=50)
    lo, hi = 15.0, 18.0
    # sweep the OPEN interior — the plan's strict lo<vix<hi excludes the endpoints
    # (outside the band you get the committed regime, not a blend)
    mus = [b.get_posterior_blended("S", "long", vix, adx=15, active_regime="R3",
                                   vix_band_lo=lo, vix_band_hi=hi,
                                   adx_threshold=25, adx_band=2).mu
           for vix in np.linspace(lo + 0.01, hi - 0.01, 11)]
    assert all(mus[i] <= mus[i + 1] + 1e-9 for i in range(len(mus) - 1))   # monotone up
    assert mus[0] == pytest.approx(0.40, abs=0.02)     # near R3 end of band
    assert mus[-1] == pytest.approx(0.70, abs=0.02)    # near R1 end of band


def test_r4_never_blended():
    b = BayesianState()
    _set(b, "S", "long", "R4", mu=0.30, n_eff=50)
    p = b.get_posterior_blended("S", "long", vix=16, adx=15, active_regime="R4",
                                vix_band_lo=15, vix_band_hi=18, adx_threshold=25, adx_band=2)
    assert p.mu == pytest.approx(0.30, abs=0.02)
