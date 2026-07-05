"""
B7d — Exec-layer ablation + pre-registered demotion rule (plan_phase3.md §B7d)

Given each arm's realised OOS returns, measure every sizing-active component's marginal
contribution and apply the demotion rule: a component keeps sizing rights only if its
ablation delta is non-negative on OOS Sharpe (or tail metrics); otherwise demote to
log-only at the next annual cycle. Reports get rationalized — this is a rule.
"""
from __future__ import annotations

import numpy as np

from reports.montecarlo import sharpe, max_drawdown


def _dd(pnls):
    return max_drawdown(10_00_000 + np.cumsum(np.asarray(pnls, dtype="float64")))


def ablation_report(arms: dict) -> dict:
    """
    arms: {"full": returns, "ms_off": returns, "ee_off": ..., "cq_off": ...,
           "all_exec_off": ..., "weighted_contra": ..., "redundancy": ...}
    Delta = full − arm; positive delta means the component EARNS its sizing rights.
    """
    if "full" not in arms:
        raise ValueError("arms must include 'full'")
    full_sh = sharpe(arms["full"])
    full_dd = _dd(arms["full"])
    out = {"full": {"sharpe": round(full_sh, 4), "max_dd": round(full_dd, 4)}}
    demotions = []
    for name, r in arms.items():
        if name == "full":
            continue
        sh, dd = sharpe(r), _dd(r)
        d_sharpe = full_sh - sh          # >0 => turning the component off HURTS => keep it
        d_dd = dd - full_dd              # >0 => turning it off worsens DD => keep it
        keep = (d_sharpe >= 0) or (d_dd >= 0)
        out[name] = {"sharpe": round(sh, 4), "delta_sharpe": round(d_sharpe, 4),
                     "delta_dd": round(d_dd, 4), "keeps_sizing_rights": keep}
        if name in ("ms_off", "ee_off", "cq_off") and not keep:
            demotions.append(name.replace("_off", ""))
    out["_demote_to_log_only"] = demotions
    return out
