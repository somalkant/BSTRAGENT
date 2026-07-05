"""
B8 — Benchmark comparison + capacity analysis (plan_phase3.md §B8)

The Bayesian system's OOS results are meaningless without context. This compares the
system's trade-return series against benchmark series (random entry, always-ORB,
always-VWAP, current production, buy&hold) on Sharpe with bootstrap significance, and
locates the capacity ceiling (where Sharpe degrades > 20% from the 10L baseline).

The benchmark trade series themselves come from running each benchmark through the same
engine/costs (a deferred heavy run); this module does the statistical comparison.
"""
from __future__ import annotations

import numpy as np

from config.settings import CAPACITY_DEGRADE


def sharpe(returns) -> float:
    r = np.asarray(returns, dtype="float64")
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1))


def bootstrap_sharpe_diff(system_returns, bench_returns, n_boot: int = 2000, seed: int = 0) -> dict:
    """Bootstrap p-value that the system's Sharpe exceeds the benchmark's."""
    rng = np.random.default_rng(seed)
    a = np.asarray(system_returns, dtype="float64")
    b = np.asarray(bench_returns, dtype="float64")
    observed = sharpe(a) - sharpe(b)
    ge = 0
    for _ in range(n_boot):
        sa = a[rng.integers(0, len(a), len(a))]
        sb = b[rng.integers(0, len(b), len(b))]
        if (sharpe(sa) - sharpe(sb)) <= 0:
            ge += 1
    p = (ge + 1) / (n_boot + 1)             # P(diff <= 0) under resampling
    return {"sharpe_system": round(sharpe(a), 4), "sharpe_bench": round(sharpe(b), 4),
            "diff": round(observed, 4), "p_value": round(p, 4), "beats": p < 0.05}


def compare_all(system_returns, benchmarks: dict, seed: int = 0) -> dict:
    """benchmarks: {name: returns}. Returns per-benchmark comparison + overall gate."""
    out = {name: bootstrap_sharpe_diff(system_returns, r, seed=seed)
           for name, r in benchmarks.items()}
    # gate: must beat random entry AND current production system (if present)
    must_beat = [k for k in ("random", "current", "current_production") if k in out]
    out["_gate_pass"] = all(out[k]["beats"] for k in must_beat) if must_beat else None
    return out


def capacity_ceiling(sharpe_by_capital: dict) -> dict:
    """
    sharpe_by_capital: {capital_rs: sharpe}. The ceiling is the largest capital whose
    Sharpe is still within CAPACITY_DEGRADE of the smallest-capital (10L) baseline.
    """
    if not sharpe_by_capital:
        return {}
    levels = sorted(sharpe_by_capital)
    base = sharpe_by_capital[levels[0]]
    ceiling = levels[0]
    for cap in levels:
        if base <= 0:
            break
        if sharpe_by_capital[cap] >= base * (1 - CAPACITY_DEGRADE):
            ceiling = cap
        else:
            break
    return {"baseline_capital": levels[0], "baseline_sharpe": round(base, 4),
            "capacity_ceiling": ceiling,
            "operating_below_ceiling": ceiling > levels[0]}
