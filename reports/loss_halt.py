"""
B5 — Loss-threshold halt & approval gate (plan_phase3.md §B5)

The single human touchpoint in the system. Execution is fully automated; when realized
losses cross a hard threshold the system halts NEW entries (protective exits keep
running) and refuses to trade the next session until explicitly approved. Halt state is
persisted so a crash cannot silently clear a halt.

  DAILY_LOSS_HALT   = 1.2 x MAX_DAILY_RISK (0.96% of capital)
  MONTHLY_LOSS_HALT = 4% of capital, rolling calendar month

Advisories (log-only, inform the approval decision, never block on their own):
  N consecutive losing trades >= 3, rolling 5-day PnL < -(2 x avg risk), active
  CHANGEPOINT / PROB_DRIFT alarms.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from config.settings import (
    CAPITAL, DAILY_LOSS_HALT, MONTHLY_LOSS_HALT, HALT_STATE_FILE,
    HALT_ADVISORY_CONSEC_LOSSES, HALT_ADVISORY_5DAY_RISK_MULT,
)


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"halted": False, "kind": None, "since": None, "approved_at": None}


def _save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _advisories(df: pd.DataFrame) -> list[str]:
    adv = []
    pnl = df["pnl_rs"].to_numpy()
    consec = 0
    for x in pnl[::-1]:
        if x <= 0:
            consec += 1
        else:
            break
    if consec >= HALT_ADVISORY_CONSEC_LOSSES:
        adv.append(f"consecutive_losses={consec}")
    if "date" in df.columns and "actual_risk" in df.columns:
        d = df.copy()
        d["date"] = pd.to_datetime(d["date"])
        last5 = d[d["date"] >= d["date"].max() - pd.Timedelta(days=5)]
        avg_risk = float(d["actual_risk"].mean() or 0)
        if avg_risk > 0 and last5["pnl_rs"].sum() < -HALT_ADVISORY_5DAY_RISK_MULT * avg_risk:
            adv.append(f"rolling_5day_pnl={last5['pnl_rs'].sum():.0f}")
    return adv


def check_loss_halt(trades_df: pd.DataFrame, capital: float = CAPITAL,
                    halt_state_path: Path | None = None, alarms: list | None = None) -> dict:
    """
    Evaluate after every trade settlement. Returns the halt state and persists it.
    trades_df must have 'date' and 'pnl_rs'.
    """
    path = Path(halt_state_path) if halt_state_path else HALT_STATE_FILE
    state = _load_state(path)
    if trades_df.empty:
        return state

    df = trades_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    last_day = df["date"].max()

    day_pnl = float(df[df["date"] == last_day]["pnl_rs"].sum())
    month_mask = (df["date"].dt.year == last_day.year) & (df["date"].dt.month == last_day.month)
    month_pnl = float(df[month_mask]["pnl_rs"].sum())

    daily_limit = -DAILY_LOSS_HALT * capital
    monthly_limit = -MONTHLY_LOSS_HALT * capital

    kind = None
    if month_pnl < monthly_limit:
        kind = "monthly"
    elif day_pnl < daily_limit:
        kind = "daily"

    advisories = _advisories(df)
    if alarms:
        advisories += [f"alarm:{a}" for a in alarms]

    if kind:
        state = {"halted": True, "kind": kind, "since": str(last_day.date()),
                 "day_pnl": round(day_pnl, 0), "month_pnl": round(month_pnl, 0),
                 "advisories": advisories, "approved_at": None}
        _save_state(state, path)
    else:
        # not tripped now; preserve any existing halt (must be explicitly approved)
        state["advisories"] = advisories
        _save_state(state, path)
    return state


def approve_resume(halt_state_path: Path | None = None, when: date | None = None) -> dict:
    """Human approval — re-arm trading. Logged with timestamp. Monthly counter persists."""
    path = Path(halt_state_path) if halt_state_path else HALT_STATE_FILE
    state = _load_state(path)
    state["halted"] = False
    state["approved_at"] = str(when or date.today())
    _save_state(state, path)
    return state


def is_halted(halt_state_path: Path | None = None) -> bool:
    path = Path(halt_state_path) if halt_state_path else HALT_STATE_FILE
    return bool(_load_state(path).get("halted"))
