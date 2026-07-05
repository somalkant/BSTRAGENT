"""
B1 seeding — bootstrap Bayesian posteriors from the old system's trade logs.

Purpose (plan_phase1.md B1 "Validation"): seed 2016–2017 (or 2016–2018) old-system
trades to (a) validate the update machinery against known strategy behaviour and
(b) give the posteriors non-degenerate mass so the Phase-1 verification run can
actually trade. Without seeding, a clean Beta(3,3) start deadlocks (posterior_scale
= 0 and driver_mu = 0.50 both block the first trade).

The plan discards seeded state before the Phase-3 clean walk-forward (B6); seeding is
a Phase-1 validation bootstrap only. Old-system trades used different gates/sizing/exits,
so this is deliberately NOT live evidence.

Only the DRIVER strategy of each old trade updates (driver-only rule, §2c).
"""
from __future__ import annotations

import logging

import pandas as pd

from config.settings import TRADE_LOG_DIR
from weights.bayesian import BayesianState

log = logging.getLogger(__name__)


def _direction(val) -> int | None:
    s = str(val).strip().upper()
    if s in ("LONG", "1", "+1", "BUY"):
        return +1
    if s in ("SHORT", "-1", "SELL"):
        return -1
    try:
        v = int(float(val))
        return v if v in (1, -1) else None
    except Exception:
        return None


def seed_from_old_trades(bayes: BayesianState, years: list[int]) -> dict:
    """Update `bayes` in place from data/trade_logs/<year>/trades.parquet. Returns a summary."""
    seeded = 0
    per_strat: dict[str, int] = {}
    for y in years:
        f = TRADE_LOG_DIR / str(y) / "trades.parquet"
        if not f.exists():
            log.warning(f"[SEED] {f} missing — skipped")
            continue
        df = pd.read_parquet(f)
        for _, row in df.iterrows():
            drv = row.get("driver_strategy")
            d = _direction(row.get("direction"))
            if not drv or d is None:
                continue
            rr = float(row.get("rr") or 0)
            entry = float(row.get("entry_price") or 0)
            stop = float(row.get("stop") or 0)
            shares = float(row.get("shares") or 0)
            pnl = float(row.get("pnl_rs") or 0)
            risk = shares * abs(entry - stop)
            if rr <= 0 or risk <= 0:
                continue
            bayes.update(drv, d, pnl_rs=pnl, risk_amount=risk, rr=rr)
            seeded += 1
            per_strat[drv] = per_strat.get(drv, 0) + 1
    log.info(f"[SEED] {seeded} old trades applied across years {years}")
    return {"seeded": seeded, "per_strategy": per_strat}
