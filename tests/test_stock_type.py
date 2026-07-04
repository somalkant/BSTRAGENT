"""
B3b validation — per-stock behavior prior (plan_phase2.md §2, exit criteria).

Run:  python -m pytest tests/test_stock_type.py -v
"""
from __future__ import annotations

import pytest

from weights.stock_type import StockTypePrior
from config.settings import STOCK_TYPE_MIN_NEFF


def test_prior_is_neutral():
    st = StockTypePrior()
    assert st.stock_type_mu("ANY") == 0.5
    assert st.n_eff("ANY") == 0.0
    assert st.get_modifier("ANY", "A") == 1.0        # thin evidence -> neutral


def test_trend_stock_from_breakout_wins():
    """A stock where breakout/trend drivers keep winning -> trend_alpha > trend_beta."""
    st = StockTypePrior()
    for _ in range(40):
        st.update("MOMO", "A", score=1.0)            # trend cluster wins
    assert st.stock_type_mu("MOMO") > 0.6
    assert st.get_modifier("MOMO", "A") > 1.0        # boosts breakout/trend cluster score
    assert st.get_modifier("MOMO", "B") < 1.0        # dampens reversion cluster score


def test_revert_stock_from_reversion_wins():
    """HDFCBANK-like: reversion drivers win -> trend_beta > trend_alpha (revert-leaning)."""
    st = StockTypePrior()
    for _ in range(40):
        st.update("HDFCBANK", "B", score=1.0)        # reversion win -> beta (revert) evidence
    assert st._state["HDFCBANK"].trend_beta > st._state["HDFCBANK"].trend_alpha
    assert st.stock_type_mu("HDFCBANK") < 0.4
    assert "revert" in st.label("HDFCBANK")


def test_modifier_neutral_below_min_neff():
    st = StockTypePrior()
    for _ in range(STOCK_TYPE_MIN_NEFF - 5):
        st.update("X", "A", score=1.0)
    assert st.get_modifier("X", "A") == 1.0          # still thin -> neutral


def test_modifier_is_two_at_mu_half():
    """×2 keeps max weight equivalent to no-modifier when stock_type_mu = 0.5."""
    st = StockTypePrior()
    for _ in range(40):                              # alternate wins/losses -> mu ~ 0.5
        st.update("BAL", "A", score=1.0)
        st.update("BAL", "A", score=0.0)
    assert st.stock_type_mu("BAL") == pytest.approx(0.5, abs=0.05)
    assert st.get_modifier("BAL", "A") == pytest.approx(1.0, abs=0.1)


def test_structure_context_clusters_never_update_or_modify():
    st = StockTypePrior()
    for _ in range(30):
        st.update("Y", "D", score=1.0)              # structure -> no personality update
        st.update("Y", "E", score=1.0)              # context -> no update
    assert st.n_eff("Y") == 0.0
    assert st.get_modifier("Y", "D") == 1.0
    assert st.get_modifier("Y", "E") == 1.0


def test_save_load_roundtrip(tmp_path):
    st = StockTypePrior()
    for _ in range(20):
        st.update("Z", "A", score=1.0)
    p = tmp_path / "stock.json"
    st.save(p)
    st2 = StockTypePrior.load(p)
    assert st2.stock_type_mu("Z") == pytest.approx(st.stock_type_mu("Z"))
    assert st2.n_eff("Z") == pytest.approx(st.n_eff("Z"))
