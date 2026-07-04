"""Shared helpers for Cluster-F meta strategies (B4c).

Meta signals are advisory VOTES (not driver-eligible), so they carry only a direction
plus nominal geometry. They fire at the first bar (meta signals are first-candle-exempt).
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

import pandas as pd
from strategies.base import BaseStrategy, Signal


def last_thursday(year: int, month: int) -> date:
    """Monthly F&O expiry = last Thursday of the month (pre-2024 convention)."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != 3:                  # 3 = Thursday
        d -= timedelta(days=1)
    return d


def in_expiry_week(d: date) -> bool:
    exp = last_thursday(d.year, d.month)
    return 0 <= (exp - d).days <= 4 and d <= exp     # Mon-Thu of the expiry week


class MetaStrategy(BaseStrategy):
    category = "meta"

    def _vote(self, direction: int, today_5min: pd.DataFrame, reason: str) -> Signal:
        if today_5min is None or today_5min.empty:
            return self._no_signal()
        first = today_5min.iloc[0]
        entry = float(first["close"])
        t = pd.Timestamp(first["datetime"]).strftime("%H:%M")
        if entry <= 0:
            return self._no_signal()
        if direction > 0:
            return self._buy(entry, entry * 1.01, entry * 0.995, signal_time=t, reason=reason)
        if direction < 0:
            return self._sell(entry, entry * 0.99, entry * 1.005, signal_time=t, reason=reason)
        return self._no_signal()
