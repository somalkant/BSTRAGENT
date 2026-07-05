"""B4c — PRE-EXPIRY (Cluster F, Meta): F&O expiry-week directional tendency. During the
monthly expiry week, votes with the prevailing 3-day trend (momentum into expiry — a
documented NSE calendar effect). Only fires in the expiry week."""
from __future__ import annotations

import pandas as pd
from strategies.base import daily_ohlcv
from strategies.meta._meta_base import MetaStrategy, in_expiry_week


class PreExpiry(MetaStrategy):
    name = "PRE-EXPIRY"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date):
        if not in_expiry_week(trade_date):
            return self._no_signal()
        if history_5min is None or history_5min.empty:
            return self._no_signal()
        daily = daily_ohlcv(history_5min)[["close"]].rename(columns={"close": "cl"}).tail(4)
        if len(daily) < 4:
            return self._no_signal()
        trend = (float(daily["cl"].iloc[-1]) - float(daily["cl"].iloc[0])) / float(daily["cl"].iloc[0])
        if trend > 0.005:
            return self._vote(+1, today_5min, f"PRE-EXPIRY: expiry-week uptrend {trend:+.2%}")
        if trend < -0.005:
            return self._vote(-1, today_5min, f"PRE-EXPIRY: expiry-week downtrend {trend:+.2%}")
        return self._no_signal()
