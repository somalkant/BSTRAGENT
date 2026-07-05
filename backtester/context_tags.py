"""
B4f — Context Layer v1 + Trade Tags (plan_phase2.md §B4f, Level 1)

Records five tags on EVERY entered trade and gated signal (the Phase 4b meta-learner's
feature set), and applies EXACTLY ONE sizing rule: breadth opposition (context_mult).
day_type / sector_rs / daily_trend are LOG-ONLY. The signal-level outcome label is
pre-registered here and read by no decision path.

Tags:
  day_type    : gap % at open + inside/outside day
  breadth     : % of point-in-time Nifty 500 members up on the day at signal time
                (advancers; supplied by the engine — a per-bar universe aggregate)
  sector_rs   : candidate sector return since open minus Nifty (supplied; log-only)
  daily_trend : close(t-1) vs daily EMA20(t-1) — prior-days-only, lookahead-free
  time_bucket : T1 09:15-10:30 | T2 10:30-13:00 | T3 13:00-15:15
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from config.settings import (
    BREADTH_LONG_MIN, BREADTH_SHORT_MAX, CONTEXT_MULT_OPPOSED,
    SIGNAL_LABEL_ATR_MULT, SIGNAL_LABEL_BARS,
)


@dataclass
class ContextTags:
    day_type:    str
    gap_pct:     float
    breadth:     float | None
    sector_rs:   float | None
    daily_trend: str
    time_bucket: str


def time_bucket(signal_time: str) -> str:
    try:
        h, m = map(int, signal_time.split(":")[:2])
        mins = h * 60 + m
    except Exception:
        return "T1"
    if mins < 10 * 60 + 30:
        return "T1"
    if mins < 13 * 60:
        return "T2"
    return "T3"


def _daily_closes(history_5min: pd.DataFrame) -> pd.Series:
    if history_5min is None or history_5min.empty:
        return pd.Series(dtype="float64")
    return history_5min.groupby(history_5min["datetime"].dt.date)["close"].last()


def compute_tags(signal, today_5min: pd.DataFrame, prev_day, history_5min,
                 breadth: float | None = None, sector_rs: float | None = None) -> ContextTags:
    # day_type — gap % and inside/outside day
    gap_pct = 0.0
    day_type = "normal"
    if prev_day is not None and not today_5min.empty:
        prev_close = float(prev_day["close"])
        today_open = float(today_5min.iloc[0]["open"])
        if prev_close > 0:
            gap_pct = round((today_open - prev_close) / prev_close * 100, 3)
        th, tl = float(today_5min["high"].max()), float(today_5min["low"].min())
        if th > float(prev_day["high"]) and tl < float(prev_day["low"]):
            day_type = "outside"
        elif th <= float(prev_day["high"]) and tl >= float(prev_day["low"]):
            day_type = "inside"

    # daily_trend — close(t-1) vs daily EMA20(t-1), prior-days only
    daily_trend = "flat"
    closes = _daily_closes(history_5min)
    if len(closes) >= 20:
        ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
        last = closes.iloc[-1]
        daily_trend = "up" if last > ema20 else "down"

    return ContextTags(day_type, gap_pct, breadth, sector_rs, daily_trend,
                       time_bucket(signal.signal_time or "09:15"))


def context_mult(direction: int, breadth: float | None) -> float:
    """The ONE B4f sizing rule — breadth opposition (plan §B4f)."""
    if breadth is None:
        return 1.0
    if direction > 0 and breadth < BREADTH_LONG_MIN:
        return CONTEXT_MULT_OPPOSED
    if direction < 0 and breadth > BREADTH_SHORT_MAX:
        return CONTEXT_MULT_OPPOSED
    return 1.0


def signal_label(signal, today_5min: pd.DataFrame, atr: float) -> float:
    """
    Pre-registered signal-level outcome label (log-only, read by no decision path):
      1   direction-consistent move >= 0.5*ATR within 12 bars
      0   opposite move >= 0.5*ATR first
      0.5 neither within 12 bars
    Written only after the window resolves.
    """
    if atr <= 0:
        return 0.5
    smin = signal.signal_time or "09:15"
    after = today_5min[today_5min["datetime"].dt.strftime("%H:%M") > smin].reset_index(drop=True)
    entry = float(signal.entry)
    thresh = SIGNAL_LABEL_ATR_MULT * atr
    for j in range(min(SIGNAL_LABEL_BARS, len(after))):
        hi, lo = float(after.iloc[j]["high"]), float(after.iloc[j]["low"])
        if signal.direction > 0:
            if hi - entry >= thresh:
                return 1.0
            if entry - lo >= thresh:
                return 0.0
        else:
            if entry - lo >= thresh:
                return 1.0
            if hi - entry >= thresh:
                return 0.0
    return 0.5
