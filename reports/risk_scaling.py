"""
B7e — Risk-scaling study (plan_phase3.md §B7e)

Re-runs the B7b Monte Carlo (and, in the full study, the S1–S7 stress suite) at risk-cap
multipliers ×1.5/×2.0/×2.5. Drawdowns scale ~linearly with the multiplier; the SAFE RAISE
CEILING is the highest multiplier at which every gate still passes. Go-live uses BASELINE
0.5%/0.8% regardless — this documents the cost of each step, it does not authorise a raise.
"""
from __future__ import annotations

import numpy as np

from config.settings import RISK_SCALE_MULTIPLIERS, DAILY_LOSS_HALT, CAPITAL
from reports.montecarlo import monte_carlo


def risk_scale_sweep(trade_pnls, multipliers=None, n_sims: int = 1000, seed: int = 0) -> dict:
    """
    Scale the realised per-trade PnL by each multiplier (risk scales ~linearly) and re-run
    the MC gates. Also flags S7's binding constraint: any single scaled loss exceeding the
    scaled daily halt fails the study regardless of aggregate Sharpe.
    """
    multipliers = multipliers or RISK_SCALE_MULTIPLIERS
    pnls = np.asarray(trade_pnls, dtype="float64")
    rows = {}
    safe_ceiling = 1.0
    for mult in [1.0] + list(multipliers):
        scaled = pnls * mult
        mc = monte_carlo(scaled, n_sims=n_sims, seed=seed)
        # S7 binding check: a single fill loss beyond the scaled daily halt
        scaled_daily_halt = DAILY_LOSS_HALT * mult * CAPITAL
        worst_single = float(-scaled.min()) if len(scaled) else 0.0
        single_fill_breach = worst_single > scaled_daily_halt
        gate_pass = bool(mc.get("pass")) and not single_fill_breach
        rows[mult] = {"mc_pass": mc.get("pass"), "p_dd_gt_15pct": mc.get("p_dd_gt_15pct"),
                      "sharpe_5pct": mc.get("sharpe_5pct"),
                      "worst_single_loss": round(worst_single, 0),
                      "scaled_daily_halt": round(scaled_daily_halt, 0),
                      "single_fill_breach": single_fill_breach, "gate_pass": gate_pass}
        if gate_pass and mult > safe_ceiling:
            safe_ceiling = mult
    return {"per_multiplier": rows, "safe_ceiling": safe_ceiling,
            "note": "go-live uses baseline caps regardless; a raise needs the B7e preconditions"}
