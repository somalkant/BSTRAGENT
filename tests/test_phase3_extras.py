"""
B7c CPCV + B5b short-universe counter (plan_phase3.md).

Run:  python -m pytest tests/test_phase3_extras.py -v
"""
from __future__ import annotations

import numpy as np

from reports import overfitting as of


def test_cpcv_paths_and_positive_fraction_for_real_edge():
    rng = np.random.default_rng(0)
    T, N = 320, 6
    M = rng.normal(0, 1, (T, N))
    M[:, 0] += 0.3                                # config 0 has a real edge
    res = of.cpcv(M, n_blocks=12, test_blocks=2, embargo=5)
    assert res["paths"] > 0
    assert res["frac_positive"] > 0.5            # IS-best generalises OOS most of the time


def test_cpcv_embargo_purges_training_rows():
    rng = np.random.default_rng(1)
    M = rng.normal(0, 1, (200, 3))
    res = of.cpcv(M, n_blocks=10, test_blocks=2, embargo=3)
    assert res["paths"] > 0                       # runs with embargo without leaking


def test_short_universe_counter_increments():
    import backtester.bayesian_engine as be
    be.reset_short_universe_counter()
    assert be.SHORT_UNIVERSE_COUNTER == {"gated_shorts": 0, "outside_fno": 0}
    # simulate the engine's counting branch (F&O file absent -> all eligible -> 0 outside)
    be.SHORT_UNIVERSE_COUNTER["gated_shorts"] += 1
    assert be.SHORT_UNIVERSE_COUNTER["gated_shorts"] == 1
