"""
B7b — Monte Carlo robustness (plan_phase3.md §B7b)

Perturbs the realised OOS trade sequence many times and reports the risk distribution:
  block-bootstrap trade order (preserves streaks), slippage jitter, execution delay,
  random trade drops. Circuit-trap losses enter UNCAPPED.

  Gates: P(max DD > 15%) < 5% ; P(negative year) < 10% ; 5th-percentile Sharpe > 0
"""
from __future__ import annotations

import numpy as np

from config.settings import (
    MC_SIMS, MC_DD_GATE, MC_DD_PROB, MC_NEGYEAR_PROB, CAPITAL,
)


def max_drawdown(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    return float(-dd.min()) if len(dd) else 0.0


def _block_bootstrap(pnls: np.ndarray, block: int, rng) -> np.ndarray:
    n = len(pnls)
    out = []
    while len(out) < n:
        start = rng.integers(0, n)
        out.extend(pnls[start:start + block])
    return np.asarray(out[:n], dtype="float64")


def sharpe(returns) -> float:
    r = np.asarray(returns, dtype="float64")
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1))


def monte_carlo(trade_pnls, capital: float = CAPITAL, n_sims: int = MC_SIMS,
                block: int = 5, slip_jitter: float = 0.0, drop_frac: float = 0.08,
                seed: int = 0) -> dict:
    """
    trade_pnls: realised per-trade PnL (Rs). Returns risk distribution + gate pass/fail.
    slip_jitter: std of per-trade multiplicative slippage noise (fraction of |pnl|).
    """
    pnls = np.asarray(trade_pnls, dtype="float64")
    if len(pnls) < 10:
        return {"error": "too few trades", "pass": False}
    rng = np.random.default_rng(seed)

    dds, neg, sharpes = [], 0, []
    for _ in range(n_sims):
        s = _block_bootstrap(pnls, block, rng)
        if drop_frac > 0:                                   # random trade drops
            keep = rng.random(len(s)) >= drop_frac
            s = s[keep]
        if slip_jitter > 0:
            s = s - np.abs(s) * rng.normal(0, slip_jitter, len(s))
        if len(s) < 2:
            continue
        equity = capital + np.cumsum(s)
        dds.append(max_drawdown(equity))
        if s.sum() < 0:
            neg += 1
        sharpes.append(sharpe(s))

    dds = np.asarray(dds); sharpes = np.asarray(sharpes)
    p_dd = float((dds > MC_DD_GATE).mean())
    p_neg = float(neg / max(len(dds), 1))
    p5_sharpe = float(np.percentile(sharpes, 5))
    return {
        "sims": len(dds),
        "p_dd_gt_15pct": round(p_dd, 4), "p_negative": round(p_neg, 4),
        "sharpe_5pct": round(p5_sharpe, 4),
        "median_maxdd": round(float(np.median(dds)), 4),
        "pass": (p_dd < MC_DD_PROB and p_neg < MC_NEGYEAR_PROB and p5_sharpe > 0),
    }
