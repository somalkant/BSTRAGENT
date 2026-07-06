"""Strategy 14: NR7 / Inside Day — narrow range day leading to explosive move."""
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class NR7InsideDay(BaseStrategy):
    name     = "NR7"
    category = "price_action"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if prev_day is None or history_5min.empty:
            return self._no_signal()

        # Build daily OHLC from history
        daily = daily_ohlcv(history_5min)[["high", "low", "close"]].tail(7)
        if len(daily) < 7:
            return self._no_signal()

        ranges = daily["high"] - daily["low"]
        today_range = float(prev_day["high"]) - float(prev_day["low"])

        # NR7: yesterday's range is smallest of last 7 days
        if today_range >= ranges.min():
            return self._no_signal()

        pdh = float(prev_day["high"])
        pdl = float(prev_day["low"])

        for _, c in today_5min.iterrows():
            if self._after_cutoff(c["datetime"]):
                break
            if c["close"] > pdh:
                return self._buy(pdh, pdh + 2 * today_range, pdl,
                                  signal_time=self._candle_time(c["datetime"]),
                                  reason=f"NR7: narrow range day, breakout above {pdh:.2f}")
            if c["close"] < pdl:
                return self._sell(pdl, pdl - 2 * today_range, pdh,
                                   signal_time=self._candle_time(c["datetime"]),
                                   reason=f"NR7: narrow range day, breakdown below {pdl:.2f}")
        return self._no_signal()
