"""
B5b — Reality stress scenarios S1–S7 (plan_phase3.md §B5b)

The plan re-executes the full WF under each scenario (a deferred heavy run). This module
provides trade-series-level approximations of each perturbation for the fast loop, plus
the pass-bar evaluation (full-period Sharpe > 0 and max DD <= STRESS_DD_MULT x unstressed).
"""
from __future__ import annotations

import numpy as np

from config.settings import STRESS_DD_MULT
from reports.montecarlo import max_drawdown, sharpe


def _equity_dd(pnls, capital=10_00_000):
    eq = capital + np.cumsum(np.asarray(pnls, dtype="float64"))
    return max_drawdown(eq)


def apply_scenario(pnls, scenario: str, avg_slippage_rs: float = 200.0, seed: int = 0):
    """Trade-series approximation of each stress scenario."""
    rng = np.random.default_rng(seed)
    p = np.asarray(pnls, dtype="float64").copy()
    if scenario == "S1":                 # 2x slippage on every fill
        return p - avg_slippage_rs
    if scenario == "S2":                 # miss the first qualifying trade each day (~drop 1/2)
        return p[1::2]
    if scenario == "S3":                 # 10% of signals randomly dropped
        return p[rng.random(len(p)) >= 0.10]
    if scenario == "S4":                 # execution delayed one bar -> haircut each entry
        return p - abs(p) * 0.05
    if scenario == "S5":                 # transaction costs +50%
        return p - abs(p) * 0.03
    if scenario == "S6":                 # exchange outage: drop ~2% of trades, force-close
        keep = rng.random(len(p)) >= 0.02
        return p[keep]
    if scenario == "S7":                 # circuit worst case: band-touchers trapped (tail loss)
        out = p.copy()
        hit = rng.random(len(p)) < 0.03
        out[hit] = -abs(out[hit]) - 3 * abs(p).mean()   # trapped => uncapped tail loss
        return out
    return p


def run_stress_suite(pnls, scenarios=("S1", "S2", "S3", "S4", "S5", "S6", "S7")) -> dict:
    base_sharpe = sharpe(pnls)
    base_dd = _equity_dd(pnls)
    out = {"unstressed": {"sharpe": round(base_sharpe, 4), "max_dd": round(base_dd, 4)}}
    all_pass = True
    for s in scenarios:
        sp = apply_scenario(pnls, s)
        sh, dd = sharpe(sp), _equity_dd(sp)
        ok = sh > 0 and dd <= STRESS_DD_MULT * base_dd
        all_pass &= ok
        out[s] = {"sharpe": round(sh, 4), "max_dd": round(dd, 4), "pass": bool(ok)}
    out["_all_pass"] = bool(all_pass)
    return out


def sensitivity_sweep(base_value: float, evaluate, pct: float = 0.25) -> dict:
    """
    Perturb a scalar constant +-pct and check `evaluate(value) -> sharpe` never flips sign.
    evaluate is supplied by the caller (a re-run wrapper). Pure helper — no engine logic.
    """
    lo, hi = base_value * (1 - pct), base_value * (1 + pct)
    s_base, s_lo, s_hi = evaluate(base_value), evaluate(lo), evaluate(hi)
    signs = {np.sign(s_base), np.sign(s_lo), np.sign(s_hi)} - {0.0}
    return {"base": s_base, "low": s_lo, "high": s_hi, "no_sign_flip": len(signs) <= 1}
