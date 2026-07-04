"""
B1 validation — Bayesian State Layer (plan_phase1.md §2a–2f, Phase 1 exit criteria).

Run:  python -m pytest tests/test_bayesian.py -v
"""
from __future__ import annotations

import logging
import math

import pytest

from weights.bayesian import BayesianState, _PRIOR_CI_WIDTH
from config.settings import SHRINK_K, BAYES_ALPHA0, BAYES_BETA0


RR = 1.85   # the plan's running example RR


# ── prior / no-history ───────────────────────────────────────────────────────

def test_no_history_mu_is_exactly_half():
    """Strategies with no trade history sit at mu = 0.50 exactly (Beta(3,3))."""
    st = BayesianState()
    post = st.get_posterior("NEVER-FIRED", "long")
    assert post.mu == 0.5
    assert post.alpha == BAYES_ALPHA0 and post.beta == BAYES_BETA0
    assert post.n_eff == 0.0


def test_prior_pwin_is_half_regardless_of_shrink():
    st = BayesianState()
    assert st.get_posterior("X", "short").p_win() == pytest.approx(0.5)


# ── winsorization (§2c) ──────────────────────────────────────────────────────

def test_winsorization_equivalence(caplog):
    """
    A synthetic +12R trade updates the posterior IDENTICALLY to a +1.85R (= target)
    trade, and logs [OUTLIER_WINSORIZED].
    """
    risk = 5_000.0
    freak = BayesianState()
    normal = BayesianState()

    with caplog.at_level(logging.INFO):
        r_freak = freak.update("BOLLINGER", "short", pnl_rs=12.3 * risk,
                               risk_amount=risk, rr=RR)
    r_normal = normal.update("BOLLINGER", "short", pnl_rs=RR * risk,
                             risk_amount=risk, rr=RR)

    assert r_freak["winsorized"] is True
    assert r_normal["winsorized"] is False
    # identical posterior mass
    assert r_freak["alpha"] == r_normal["alpha"]
    assert r_freak["beta"] == r_normal["beta"]
    assert r_freak["score"] == r_normal["score"] == 1.0
    # tail event stays visible in the log
    assert any("OUTLIER_WINSORIZED" in rec.message for rec in caplog.records)


def test_winsorization_floor_on_freak_loss():
    """A −8R gap-through loss is floored at −1.5R for the posterior only."""
    risk = 5_000.0
    st = BayesianState()
    res = st.update("X", "long", pnl_rs=-8.0 * risk, risk_amount=risk, rr=RR)
    # score at raw=-1.5R: (-1.5+1)/(1.85+1) = -0.5/2.85
    expected = max(0.0, (-1.5 + 1) / (RR + 1))
    assert res["winsorized"] is True
    assert res["score"] == pytest.approx(round(expected, 4))


def test_score_mapping_examples():
    """§2c worked examples at RR=1.85."""
    st = BayesianState()
    full, _, _ = st.score_from_pnl(RR * 1.0, 1.0, RR)      # full target
    be, _, _ = st.score_from_pnl(0.0, 1.0, RR)             # break-even
    stop, _, _ = st.score_from_pnl(-1.0, 1.0, RR)          # full stop
    assert full == pytest.approx(1.0)
    assert be == pytest.approx(1.0 / (RR + 1), abs=1e-9)   # 0.35
    assert stop == pytest.approx(0.0)


# ── model-uncertainty shrinkage (§2f) ────────────────────────────────────────

def _make_posterior(st, strategy, direction, mu, n_eff):
    """Directly construct a cell with a target mu and n_eff for controlled tests."""
    cell = st._cell(strategy, direction)
    # choose alpha/beta on the same total mass as the prior scaled so mu matches;
    # n_eff is tracked independently, set it directly.
    total = 20.0
    cell.alpha = mu * total
    cell.beta = (1 - mu) * total
    cell.n_eff = n_eff
    return st.get_posterior(strategy, direction)


def test_shrinkage_pulls_low_evidence_to_neutral():
    """
    At n_eff = 10 the shrink weight is 0.25, so a strong measured mu barely moves
    P(win) off 0.50 (§2f). For a realistic mu≈0.60 it stays within 0.03 of 0.50.
    """
    st = BayesianState()
    w = 10.0 / (10.0 + SHRINK_K)
    assert w == pytest.approx(0.25)

    post = _make_posterior(st, "S", "long", mu=0.60, n_eff=10.0)
    assert post.mu == pytest.approx(0.60)
    assert abs(post.p_win() - 0.50) <= 0.03          # exit criterion

    # shrinkage removes (1-w)=75% of the deviation, whatever the raw mu
    for raw_mu in (0.55, 0.63, 0.70, 0.81):
        p = _make_posterior(st, "S", "long", mu=raw_mu, n_eff=10.0).p_win()
        assert abs(p - 0.50) == pytest.approx(w * (raw_mu - 0.50), rel=1e-6)


def test_shrinkage_fades_with_evidence():
    """High n_eff → posterior mostly trusted (§2f table)."""
    st = BayesianState()
    for n_eff, w_exp in [(10, 0.25), (60, 0.667), (200, 0.869)]:
        post = _make_posterior(st, "S", "long", mu=0.63, n_eff=float(n_eff))
        w = n_eff / (n_eff + SHRINK_K)
        assert w == pytest.approx(w_exp, abs=1e-3)
        assert post.p_win() == pytest.approx(w * 0.63 + (1 - w) * 0.5)


# ── EV / Kelly (§2f, §2i) ────────────────────────────────────────────────────

def test_ev_and_kelly_formulas():
    st = BayesianState()
    post = _make_posterior(st, "S", "long", mu=0.644, n_eff=1e6)  # ~no shrink
    p = post.p_win()
    assert p == pytest.approx(0.644, abs=1e-3)
    ev = post.ev(RR)
    assert ev == pytest.approx(p * (RR + 1) - 1, abs=1e-9)
    assert post.kelly(RR) == pytest.approx(ev / RR, abs=1e-9)


# ── decay (§2d) ──────────────────────────────────────────────────────────────

def test_decay_downweights_old_evidence():
    """n_eff follows the geometric series (1-DECAY^N)/(1-DECAY), ceiling 1/(1-DECAY)."""
    decay, n = 0.999, 2000
    st = BayesianState(decay=decay)
    for _ in range(n):
        st.update("S", "long", pnl_rs=1.85 * 5000, risk_amount=5000, rr=RR)
    post = st.get_posterior("S", "long")
    expected = (1 - decay ** n) / (1 - decay)          # ~865 at N=2000
    assert post.n_eff == pytest.approx(expected, rel=1e-6)
    assert post.n_eff < 1.0 / (1 - decay)              # strictly below the ~1000 ceiling


# ── machinery sanity: strong vs weak converge correctly ──────────────────────

def test_strong_and_weak_strategies_separate():
    """
    Feeding a strong (70% full-target) vs weak (20% full-target) stream drives
    mu above 0.60 and below 0.50 respectively — the seeding sanity check, done on
    the clean machinery instead of discarded old-system logs.
    """
    strong = BayesianState()
    weak = BayesianState()
    risk = 5000.0
    # deterministic 70/30 and 20/80 win streams
    for i in range(120):
        strong.update("ORB-15", "long",
                      pnl_rs=(RR if i % 10 < 7 else -1) * risk, risk_amount=risk, rr=RR)
        weak.update("DESC-TRI", "short",
                    pnl_rs=(RR if i % 10 < 2 else -1) * risk, risk_amount=risk, rr=RR)
    assert strong.mu("ORB-15", "long") > 0.60
    assert weak.mu("DESC-TRI", "short") < 0.50


# ── persistence ──────────────────────────────────────────────────────────────

def test_save_load_roundtrip(tmp_path):
    st = BayesianState()
    st.update("BOLLINGER", "short", pnl_rs=1.85 * 5000, risk_amount=5000, rr=RR)
    st.update("BOLLINGER", "short", pnl_rs=-5000, risk_amount=5000, rr=RR)
    p = tmp_path / "bayes.json"
    st.save(p)
    st2 = BayesianState.load(p)
    a = st.get_posterior("BOLLINGER", "short")
    b = st2.get_posterior("BOLLINGER", "short")
    assert (a.alpha, a.beta, a.n_eff) == (b.alpha, b.beta, b.n_eff)


def test_prior_ci_width_positive():
    assert 0 < _PRIOR_CI_WIDTH < 1
