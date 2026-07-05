"""B4b — RSI-DIV (Cluster B): price vs RSI divergence. RSI-EXT fires at extremes; this
fires on weakening momentum (divergence) before the reversal."""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta
from strategies.base import BaseStrategy, Signal
from strategies.reversion._divergence import find_divergence


class RSIDivergence(BaseStrategy):
    name     = "RSI-DIV"
    category = "reversion"
    RSI_LEN  = 14

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        combined = pd.concat([history_5min.tail(120), today_5min]).reset_index(drop=True)
        if len(combined) < self.RSI_LEN + 25:
            return self._no_signal()
        rsi = ta.rsi(combined["close"], length=self.RSI_LEN)
        start = len(combined) - len(today_5min)

        for i in range(start, len(combined)):
            c = combined.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            d = find_divergence(combined["close"], rsi, i)
            if d == 0:
                continue
            entry = float(c["close"])
            atr = float((combined["high"] - combined["low"]).iloc[max(0, i - 14):i].mean())
            if atr <= 0:
                continue
            if d == +1:
                return self._buy(entry, entry + 1.8 * atr, entry - atr,
                                 signal_time=self._candle_time(c["datetime"]),
                                 reason="RSI-DIV: bullish divergence")
            return self._sell(entry, entry - 1.8 * atr, entry + atr,
                              signal_time=self._candle_time(c["datetime"]),
                              reason="RSI-DIV: bearish divergence")
        return self._no_signal()
