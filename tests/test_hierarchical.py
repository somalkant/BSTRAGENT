"""
B3c validation — hierarchical cluster priors (plan_phase2.md §3, exit criteria).

Run:  python -m pytest tests/test_hierarchical.py -v
"""
from __future__ import annotations

import pytest

from weights.bayesian import BayesianState
from config.settings import K_HIER

# real cluster members (config/strategy_clusters.json): B = reversion
B1, B2 = "BOLLINGER", "RSI-EXT"       # both cluster B
A1 = "ORB-15"                          # cluster A


def _win_stream(b, strat, d, n, win=True):
    pnl = 1.85 * 5000 if win else -5000
    for _ in range(n):
        b.update(strat, d, pnl_rs=pnl, risk_amount=5000, rr=1.85)


def test_new_strategy_starts_near_cluster_mean_not_half():
    """A fresh strategy in an established cluster inherits the family, not 0.50."""
    b = BayesianState()
    _win_stream(b, B1, "short", 60, win=True)          # BOLLINGER builds cluster B up
    p_new = b.get_posterior(B2, "short")               # RSI-EXT: zero own evidence
    assert p_new.n_eff == 0.0
    assert p_new.mu == pytest.approx(0.5)              # raw is still prior
    assert p_new.mu_cluster is not None and p_new.mu_cluster > 0.6
    assert p_new.mu_hier > 0.58                        # shrunk toward the winning cluster
    assert p_new.w_hier == pytest.approx(0.0, abs=1e-9)


def test_established_strategy_barely_shifted():
    """High-n_eff strategy in a coherent cluster: |mu_hier - mu_raw| < 0.02 — its own
    evidence dominates (w_hier > 0.8), so the family barely nudges it."""
    b = BayesianState()
    _win_stream(b, B1, "short", 200, win=True)         # BOLLINGER huge evidence
    _win_stream(b, B2, "short", 40, win=True)          # coherent family (also winning)
    p = b.get_posterior(B1, "short")
    assert p.w_hier > 0.8
    assert abs(p.mu_hier - p.mu) < 0.02

    # and the shift shrinks as own-evidence grows (convergence), even if the family diverges
    b2 = BayesianState()
    _win_stream(b2, B1, "short", 40, win=True)
    _win_stream(b2, B2, "short", 40, win=False)        # divergent family
    shift_small_n = abs(b2.get_posterior(B1, "short").mu_hier - b2.get_posterior(B1, "short").mu)
    _win_stream(b2, B1, "short", 400, win=True)        # now B1 dominates
    shift_big_n = abs(b2.get_posterior(B1, "short").mu_hier - b2.get_posterior(B1, "short").mu)
    assert shift_big_n < shift_small_n


def test_unmapped_strategy_no_hierarchy():
    b = BayesianState()
    _win_stream(b, "NOT-IN-MAP", "long", 30, win=True)
    p = b.get_posterior("NOT-IN-MAP", "long")
    assert p.mu_cluster is None
    assert p.mu_hier == p.mu


def test_pool_reflects_only_member_evidence_no_double_count():
    """Cluster pool = sum of member evidence; removing a member's trades changes it."""
    b_both = BayesianState()
    _win_stream(b_both, B1, "short", 40, win=True)
    _win_stream(b_both, B2, "short", 40, win=False)
    mu_both = b_both.get_posterior(B1, "short").mu_cluster

    b_one = BayesianState()
    _win_stream(b_one, B1, "short", 40, win=True)       # only B1
    mu_one = b_one.get_posterior(B1, "short").mu_cluster
    assert mu_one > mu_both                              # dropping B2's losses raises pool mu


def test_p_win_uses_hierarchical_mean():
    b = BayesianState()
    _win_stream(b, B1, "short", 60, win=True)
    p_new = b.get_posterior(B2, "short")               # new member, mu_hier > 0.5
    # p_win shrinks mu_hier toward 0.5 by n_eff (0) -> stays 0.5 for a zero-evidence member
    assert p_new.p_win() == pytest.approx(0.5, abs=1e-9)
    # but a member with its own evidence + cluster tailwind exceeds 0.5
    _win_stream(b, B2, "short", 40, win=True)
    assert b.get_posterior(B2, "short").p_win() > 0.55


def test_save_load_preserves_pool(tmp_path):
    b = BayesianState()
    _win_stream(b, B1, "short", 30, win=True)
    p = tmp_path / "bayes.json"
    b.save(p)
    b2 = BayesianState.load(p)
    assert b2.get_posterior(B1, "short").mu_cluster == pytest.approx(
        b.get_posterior(B1, "short").mu_cluster)
