"""B4d — MTF-15M-EMA (Cluster G, Multi-TF): EMA 9/21 crossover confirmed on the 15-min
chart. Cross-timeframe confirmation independent of the 5-min EMA-CROSS."""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta
from strategies.base import BaseStrategy, Signal
from strategies.multiframe._mtf import to_15m


class MTF15mEMA(BaseStrategy):
    name     = "MTF-15M-EMA"
    category = "multiframe"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        combined = pd.concat([history_5min.tail(300), today_5min]) if history_5min is not None \
            else today_5min
        bars = to_15m(combined)
        if len(bars) < 25:
            return self._no_signal()
        ema9 = ta.ema(bars["close"], length=9)
        ema21 = ta.ema(bars["close"], length=21)
        atr = ta.atr(bars["high"], bars["low"], bars["close"], length=14)

        today_bars = to_15m(today_5min)
        if today_bars.empty:
            return self._no_signal()
        first_today = today_bars["datetime"].iloc[0]
        start = bars.index[bars["datetime"] >= first_today]
        if len(start) == 0:
            return self._no_signal()

        for i in range(max(1, int(start[0])), len(bars)):
            c = bars.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            if pd.isna(ema9.iloc[i]) or pd.isna(ema21.iloc[i]) or pd.isna(atr.iloc[i]):
                continue
            cross_up = ema9.iloc[i] > ema21.iloc[i] and ema9.iloc[i - 1] <= ema21.iloc[i - 1]
            cross_dn = ema9.iloc[i] < ema21.iloc[i] and ema9.iloc[i - 1] >= ema21.iloc[i - 1]
            entry, a = float(c["close"]), float(atr.iloc[i])
            if cross_up:
                return self._buy(entry, entry + 2 * a, entry - a,
                                 signal_time=self._candle_time(c["datetime"]),
                                 reason="MTF-15M-EMA: 15m EMA9>EMA21 cross")
            if cross_dn:
                return self._sell(entry, entry - 2 * a, entry + a,
                                  signal_time=self._candle_time(c["datetime"]),
                                  reason="MTF-15M-EMA: 15m EMA9<EMA21 cross")
        return self._no_signal()
