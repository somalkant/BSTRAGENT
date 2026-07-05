"""
B5 — Calibration & drift reporting (plan_phase3.md §B5)

Reads a trades DataFrame (paper_trades.csv shape) and computes the Bayesian-specific
metrics: ECE, Brier, EV-realisation, probability-drift alarm, strategy importance,
tag breakdowns, loss-decomposition / win-capture, and config integrity. No backtesting
dependency — runs any time on existing logs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import (
    PWIN_BINS, PROB_DRIFT_WINDOW, PROB_DRIFT_ALARM, ECE_TARGET, BRIER_TARGET,
    EV_REALISATION_MIN, IMPORTANCE_MIN_NEFF, IMPORTANCE_LOOKBACK_MONTHS,
)


# ── helpers ───────────────────────────────────────────────────────────────────
def _win(df: pd.DataFrame) -> pd.Series:
    return (df["pnl_rs"] > 0).astype(float)


def _pwin(df: pd.DataFrame) -> pd.Series:
    col = "driver_p" if "driver_p" in df.columns else "predicted_win_pct"
    p = df[col].astype(float)
    return p / 100.0 if p.max() > 1.5 else p          # accept % or fraction


def _realized_r(df: pd.DataFrame) -> pd.Series:
    risk = df["actual_risk"].replace(0, np.nan) if "actual_risk" in df.columns else np.nan
    return (df["pnl_rs"] / risk).replace([np.inf, -np.inf], np.nan)


# ── metrics ───────────────────────────────────────────────────────────────────
def ece(df: pd.DataFrame) -> dict:
    """Expected Calibration Error: Σ |pred_wr − actual_wr| × (n_bin/n)."""
    if df.empty:
        return {"ece": None, "bins": []}
    p, win = _pwin(df), _win(df)
    n = len(df)
    total = 0.0
    bins = []
    for lo, hi in zip(PWIN_BINS[:-1], PWIN_BINS[1:]):
        m = (p >= lo) & (p < hi)
        nb = int(m.sum())
        if nb == 0:
            continue
        pred = float(p[m].mean())
        act = float(win[m].mean())
        total += abs(pred - act) * nb / n
        bins.append({"bin": f"[{lo:.2f},{hi:.2f})", "n": nb, "pred_wr": round(pred, 3),
                     "actual_wr": round(act, 3), "gap": round(pred - act, 3)})
    return {"ece": round(total, 4), "pass": total < ECE_TARGET, "bins": bins}


def brier(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"brier": None}
    p, win = _pwin(df), _win(df)
    b = float(((p - win) ** 2).mean())
    return {"brier": round(b, 4), "pass": b < BRIER_TARGET}


def ev_realisation(df: pd.DataFrame) -> dict:
    if df.empty or "ev" not in df.columns:
        return {"ratio": None}
    predicted = float(df["ev"].astype(float).mean())
    realised = float(_realized_r(df).mean())
    ratio = realised / predicted if predicted else None
    return {"predicted_ev": round(predicted, 4), "realised_r": round(realised, 4),
            "ratio": round(ratio, 3) if ratio is not None else None,
            "pass": (ratio is not None and ratio >= EV_REALISATION_MIN)}


def prob_drift(df: pd.DataFrame) -> list[dict]:
    """Rolling PROB_DRIFT_WINDOW-trade |mean predicted − realised win rate|; flag > alarm."""
    if len(df) < PROB_DRIFT_WINDOW:
        return []
    p, win = _pwin(df).to_numpy(), _win(df).to_numpy()
    alarms = []
    for i in range(PROB_DRIFT_WINDOW, len(df) + 1):
        w = slice(i - PROB_DRIFT_WINDOW, i)
        gap = abs(p[w].mean() - win[w].mean())
        if gap > PROB_DRIFT_ALARM:
            alarms.append({"end_idx": i - 1, "predicted": round(float(p[w].mean()), 3),
                           "realised": round(float(win[w].mean()), 3), "gap": round(float(gap), 3)})
    return alarms


def strategy_importance(df: pd.DataFrame, neff: dict | None = None) -> pd.DataFrame:
    """Per strategy: trades, realised R, mean EV. Flags established (n_eff>40) negatives."""
    if df.empty:
        return pd.DataFrame()
    g = df.assign(realized_r=_realized_r(df)).groupby("driver_strategy")
    out = g.agg(trades=("pnl_rs", "size"), realised_r=("realized_r", "sum"),
                mean_ev=("ev", "mean") if "ev" in df.columns else ("pnl_rs", "size")).reset_index()
    out["flag"] = out.apply(
        lambda r: (neff and neff.get(r["driver_strategy"], 0) > IMPORTANCE_MIN_NEFF
                   and r["realised_r"] < 0), axis=1)
    return out.sort_values("realised_r")


def tag_breakdown(df: pd.DataFrame) -> dict:
    out = {}
    for tag in ("time_bucket", "day_type"):
        if tag in df.columns:
            g = df.assign(r=_realized_r(df), win=_win(df)).groupby(tag)
            out[tag] = g.agg(trades=("pnl_rs", "size"), win_rate=("win", "mean"),
                             realised_r=("r", "sum")).round(3).reset_index().to_dict("records")
    if "breadth" in df.columns:
        b = df.dropna(subset=["breadth"]).copy()
        if not b.empty:
            b["band"] = pd.cut(b["breadth"].astype(float), [0, 0.3, 0.7, 1.01],
                               labels=["<30%", "30-70%", ">70%"])
            g = b.assign(r=_realized_r(b), win=_win(b)).groupby("band", observed=True)
            out["breadth"] = g.agg(trades=("pnl_rs", "size"), win_rate=("win", "mean"),
                                   realised_r=("r", "sum")).round(3).reset_index().to_dict("records")
    return out


def loss_decomposition(df: pd.DataFrame) -> dict:
    """never-worked / gave-back / truncated for losers; capture ratio for winners (B2 excursion)."""
    if df.empty or "mfe_r" not in df.columns:
        return {}
    losers = df[df["pnl_rs"] <= 0]
    winners = df[df["pnl_rs"] > 0]
    never = int((losers["mfe_r"].astype(float) < 0.3).sum())
    gave_back = int(((losers["mfe_r"].astype(float) > 1.0)).sum())
    truncated = int(((losers.get("exit_reason") == "EOD") & (losers["mfe_r"].astype(float) > 0)).sum())
    cap = None
    if not winners.empty:
        r = _realized_r(winners) / winners["mfe_r"].replace(0, np.nan).astype(float)
        cap = round(float(r.replace([np.inf, -np.inf], np.nan).dropna().mean()), 3)
    return {"losers": len(losers), "never_worked": never, "gave_back": gave_back,
            "truncated": truncated, "winners": len(winners), "capture_ratio": cap}


def config_integrity(df: pd.DataFrame) -> dict:
    if "settings_hash" not in df.columns:
        return {"present_pct": 0.0, "drift": None}
    present = float(df["settings_hash"].notna().mean())
    hashes = df["settings_hash"].dropna().unique().tolist()
    return {"present_pct": round(present * 100, 1), "n_hashes": len(hashes),
            "config_drift": len(hashes) > 1, "hashes": hashes[:5]}


def posterior_width_convergence(state_start, state_end, direction="long") -> pd.DataFrame:
    """95% CI width per strategy at window start vs end (shrinking = learning)."""
    from scipy.stats import beta as bdist
    rows = []
    strategies = set(state_start._state) | set(state_end._state)
    for s in sorted(strategies):
        def width(st):
            c = st._state.get(s, {}).get(direction)
            if c is None:
                return None
            return float(bdist.ppf(0.975, c.alpha, c.beta) - bdist.ppf(0.025, c.alpha, c.beta))
        w0, w1 = width(state_start), width(state_end)
        if w0 is not None and w1 is not None:
            rows.append({"strategy": s, "ci_start": round(w0, 4), "ci_end": round(w1, 4),
                         "shrank": w1 < w0})
    return pd.DataFrame(rows)


# ── report generation ─────────────────────────────────────────────────────────
def generate_report(df: pd.DataFrame, neff: dict | None = None) -> dict:
    """Bundle all metrics into one dict (the per-window calibration summary)."""
    return {
        "n_trades": len(df),
        "ece": ece(df), "brier": brier(df), "ev_realisation": ev_realisation(df),
        "prob_drift_alarms": len(prob_drift(df)),
        "loss_decomposition": loss_decomposition(df),
        "config_integrity": config_integrity(df),
        "tag_breakdown": tag_breakdown(df),
    }
