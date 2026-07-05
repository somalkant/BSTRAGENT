"""
B7a — False-discovery / backtest-overfitting control (plan_phase3.md §B7a)

With 53 strategies × directions × regimes, thousands of configs were implicitly searched.
A good-looking WF curve is not proof of edge. This module deflates for that:
  - Deflated Sharpe Ratio (DSR)  — gate: > 0.95
  - Probability of Backtest Overfitting (PBO) via CSCV — gate: < 0.20
  - White's Reality Check (bootstrap) — system vs best random strategy from the same universe
"""
from __future__ import annotations

import itertools

import numpy as np
from scipy.stats import norm, skew as _skew, kurtosis as _kurt

from config.settings import DSR_CONFIDENCE, PBO_GATE

_EULER = 0.5772156649015329


def sharpe(returns) -> float:
    r = np.asarray(returns, dtype="float64")
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1))


def _psr(sr, n, sk, ku, sr_benchmark=0.0) -> float:
    """Probabilistic Sharpe Ratio — P(true SR > benchmark) given skew/kurtosis."""
    den = np.sqrt(max(1e-12, 1 - sk * sr + (ku - 1) / 4.0 * sr ** 2))
    return float(norm.cdf((sr - sr_benchmark) * np.sqrt(max(n - 1, 1)) / den))


def _expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max SR] under the null of n_trials independent zero-edge strategies."""
    n_trials = max(2, int(n_trials))
    z1 = norm.ppf(1 - 1.0 / n_trials)
    z2 = norm.ppf(1 - 1.0 / (n_trials * np.e))
    return float(np.sqrt(max(var_sr, 1e-12)) * ((1 - _EULER) * z1 + _EULER * z2))


def deflated_sharpe(returns, n_trials: int) -> dict:
    """DSR = PSR against the expected-max-Sharpe benchmark for n_trials (Bailey & LdP)."""
    r = np.asarray(returns, dtype="float64")
    n = len(r)
    if n < 3:
        return {"dsr": None, "sharpe": None, "pass": False}
    sr = sharpe(r)
    sk = float(_skew(r))
    ku = float(_kurt(r, fisher=False))
    var_sr = (1 - sk * sr + (ku - 1) / 4.0 * sr ** 2) / (n - 1)
    sr0 = _expected_max_sharpe(n_trials, var_sr)
    dsr = _psr(sr, n, sk, ku, sr_benchmark=sr0)
    return {"dsr": round(dsr, 4), "sharpe": round(sr, 4), "sr_benchmark": round(sr0, 4),
            "n_trials": n_trials, "pass": dsr > DSR_CONFIDENCE}


def pbo_cscv(returns_matrix, n_blocks: int = 16) -> dict:
    """
    Probability of Backtest Overfitting via Combinatorially-Symmetric Cross-Validation.
    returns_matrix: (T periods x N configs). PBO = P(IS-best config underperforms OOS median).
    """
    M = np.asarray(returns_matrix, dtype="float64")
    T, N = M.shape
    if N < 2 or n_blocks < 4 or T < n_blocks:
        return {"pbo": None, "pass": False}
    blocks = np.array_split(np.arange(T), n_blocks)
    half = n_blocks // 2
    logits = []
    for combo in itertools.combinations(range(n_blocks), half):
        is_idx = np.concatenate([blocks[b] for b in combo])
        oos_idx = np.concatenate([blocks[b] for b in range(n_blocks) if b not in combo])
        is_sr = np.array([sharpe(M[is_idx, c]) for c in range(N)])
        oos_sr = np.array([sharpe(M[oos_idx, c]) for c in range(N)])
        best = int(np.argmax(is_sr))
        # rank of IS-best in OOS (0..1); overfit if below median
        rank = (oos_sr < oos_sr[best]).mean()
        rank = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(np.log(rank / (1 - rank)))
    logits = np.asarray(logits)
    pbo = float((logits <= 0).mean())               # fraction where OOS rank <= median
    return {"pbo": round(pbo, 4), "n_splits": len(logits), "pass": pbo < PBO_GATE}


def cpcv(returns_matrix, n_blocks: int = 16, test_blocks: int = 2, embargo: int = 5) -> dict:
    """
    B7c — Combinatorial Purged Cross-Validation (plan_phase3.md §B7c). Builds many
    train/test paths from the OOS span with an embargo gap around each test block so
    overlapping positions / decayed posteriors don't leak. Reports the OOS Sharpe
    distribution of the in-sample-best config (informational this cycle).
    """
    M = np.asarray(returns_matrix, dtype="float64")
    T, N = M.shape
    if N < 2 or T < n_blocks:
        return {"paths": 0, "oos_sharpe_median": None}
    blocks = np.array_split(np.arange(T), n_blocks)
    oos_sharpes = []
    for combo in itertools.combinations(range(n_blocks), test_blocks):
        test_idx = np.concatenate([blocks[b] for b in combo])
        lo, hi = test_idx.min() - embargo, test_idx.max() + embargo   # purge + embargo
        train_idx = np.array([i for i in range(T)
                              if i not in set(test_idx) and not (lo <= i <= hi)])
        if len(train_idx) < n_blocks:
            continue
        is_sr = np.array([sharpe(M[train_idx, c]) for c in range(N)])
        best = int(np.argmax(is_sr))
        oos_sharpes.append(sharpe(M[test_idx, best]))
    if not oos_sharpes:
        return {"paths": 0, "oos_sharpe_median": None}
    arr = np.asarray(oos_sharpes)
    return {"paths": len(arr), "oos_sharpe_median": round(float(np.median(arr)), 4),
            "oos_sharpe_5pct": round(float(np.percentile(arr, 5)), 4),
            "frac_positive": round(float((arr > 0).mean()), 4)}


def whites_reality_check(system_returns, strategy_returns_matrix, n_boot: int = 1000,
                         seed: int = 0) -> dict:
    """
    Bootstrap: is the system's mean return beyond the best of the null strategy universe?
    Returns a p-value (small = system genuinely beats the best-random benchmark).
    """
    rng = np.random.default_rng(seed)
    sysr = np.asarray(system_returns, dtype="float64")
    S = np.asarray(strategy_returns_matrix, dtype="float64")   # T x N
    T = len(sysr)
    observed = sysr.mean() - S.mean(axis=0).max()
    count = 0
    for _ in range(n_boot):
        idx = rng.integers(0, T, T)
        boot_sys = sysr[idx].mean() - sysr.mean()
        boot_best = (S[idx].mean(axis=0) - S.mean(axis=0)).max()
        if (boot_sys - boot_best) >= observed:
            count += 1
    p = (count + 1) / (n_boot + 1)
    return {"observed_edge": round(float(observed), 6), "p_value": round(p, 4), "pass": p < 0.05}
