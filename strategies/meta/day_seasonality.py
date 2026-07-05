"""B4c — DAY-SEASONALITY (Cluster F, Meta): day-of-week bias learned from this stock's
own recent same-weekday returns (lookahead-free — history only). Temporal pattern,
orthogonal to price signals."""
from __future__ import annotations

import pandas as pd
from strategies.meta._meta_base import MetaStrategy

THRESH = 0.003     # avg same-weekday return magnitude to vote


class DaySeasonality(MetaStrategy):
    name = "DAY-SEASONALITY"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date):
        if history_5min is None or history_5min.empty:
            return self._no_signal()
        h = history_5min.copy()
        daily = h.groupby(h["datetime"].dt.date).agg(op=("open", "first"), cl=("close", "last"))
        daily["ret"] = (daily["cl"] - daily["op"]) / daily["op"]
        daily["dow"] = [d.weekday() for d in daily.index]
        same = daily[daily["dow"] == trade_date.weekday()]["ret"].tail(8)
        if len(same) < 4:
            return self._no_signal()
        avg = float(same.mean())
        if avg > THRESH:
            return self._vote(+1, today_5min, f"DAY-SEASONALITY: {trade_date.strftime('%a')} avg {avg:+.2%}")
        if avg < -THRESH:
            return self._vote(-1, today_5min, f"DAY-SEASONALITY: {trade_date.strftime('%a')} avg {avg:+.2%}")
        return self._no_signal()
