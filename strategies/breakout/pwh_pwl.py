"""B4a — PWH-PWL: Prior Week High/Low breakout (Cluster A). Weekly levels carry more
weight than the prior day; complements PDH-PDL."""
from __future__ import annotations

import pandas as pd
from strategies.base import BaseStrategy, Signal


class PriorWeekBreakout(BaseStrategy):
    name     = "PWH-PWL"
    category = "breakout"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min is None or history_5min.empty or today_5min.empty:
            return self._no_signal()

        # prior calendar week = the ISO week before trade_date's week
        hist = history_5min.copy()
        hist["d"] = hist["datetime"].dt.date
        iso = hist["datetime"].dt.isocalendar()
        this_year, this_week, _ = trade_date.isocalendar()
        prior = hist[(iso["week"] != this_week) | (iso["year"] != this_year)]
        if prior.empty:
            return self._no_signal()
        # take the most recent completed week block
        last_week_key = (iso["year"].astype(str) + "-" + iso["week"].astype(str))
        prior_keys = last_week_key[(iso["year"] < this_year) |
                                   ((iso["year"] == this_year) & (iso["week"] < this_week))]
        if prior_keys.empty:
            return self._no_signal()
        target_key = prior_keys.iloc[-1]
        mask = last_week_key == target_key
        pwh = float(hist.loc[mask, "high"].max())
        pwl = float(hist.loc[mask, "low"].min())
        width = pwh - pwl
        if width <= 0:
            return self._no_signal()

        for _, c in today_5min.iterrows():
            if self._after_cutoff(c["datetime"]):
                break
            if c["close"] > pwh and c["volume"] > 0:
                return self._buy(pwh, pwh + width, pwl,
                                 signal_time=self._candle_time(c["datetime"]),
                                 reason=f"PWH-PWL: breakout above PWH={pwh:.2f}")
            if c["close"] < pwl and c["volume"] > 0:
                return self._sell(pwl, pwl - width, pwh,
                                  signal_time=self._candle_time(c["datetime"]),
                                  reason=f"PWH-PWL: breakdown below PWL={pwl:.2f}")
        return self._no_signal()
