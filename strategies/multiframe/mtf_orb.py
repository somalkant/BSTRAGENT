"""B4d — MTF-15M-ORB (Cluster G, Multi-TF): opening-range breakout confirmed on the
15-min chart. Independent timeframe from the 5-min ORB."""
from __future__ import annotations

import pandas as pd
from strategies.base import BaseStrategy, Signal
from strategies.multiframe._mtf import to_15m


class MTF15mORB(BaseStrategy):
    name     = "MTF-15M-ORB"
    category = "multiframe"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        bars = to_15m(today_5min)
        if len(bars) < 3:
            return self._no_signal()
        or_bar = bars.iloc[0]                       # 09:15-09:30 opening range
        orh, orl = float(or_bar["high"]), float(or_bar["low"])
        width = orh - orl
        if width <= 0:
            return self._no_signal()

        for i in range(1, len(bars)):
            c = bars.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            close = float(c["close"])
            if close > orh:
                return self._buy(close, close + width, orl,
                                 signal_time=self._candle_time(c["datetime"]),
                                 reason=f"MTF-15M-ORB: 15m close above OR high {orh:.2f}")
            if close < orl:
                return self._sell(close, close - width, orh,
                                  signal_time=self._candle_time(c["datetime"]),
                                  reason=f"MTF-15M-ORB: 15m close below OR low {orl:.2f}")
        return self._no_signal()
