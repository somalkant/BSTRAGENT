"""B4a — TTM-SQUEEZE: Bollinger Bands inside Keltner Channels = coiled spring (Cluster A).
Fires on the squeeze RELEASE, in the direction of momentum. Bollinger alone can't detect
the compression condition."""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta
from strategies.base import BaseStrategy, Signal


class TTMSqueeze(BaseStrategy):
    name     = "TTM-SQUEEZE"
    category = "breakout"
    LEN      = 20
    BB_STD   = 2.0
    KC_MULT  = 1.5

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        combined = pd.concat([history_5min.tail(150), today_5min]).reset_index(drop=True)
        if len(combined) < self.LEN + 5:
            return self._no_signal()

        bb = ta.bbands(combined["close"], length=self.LEN, std=self.BB_STD)
        atr = ta.atr(combined["high"], combined["low"], combined["close"], length=self.LEN)
        if bb is None or atr is None:
            return self._no_signal()
        bbl = bb[[c for c in bb.columns if "BBL" in c][0]]
        bbu = bb[[c for c in bb.columns if "BBU" in c][0]]
        mid = combined["close"].rolling(self.LEN).mean()
        kcu = mid + self.KC_MULT * atr
        kcl = mid - self.KC_MULT * atr
        squeeze_on = (bbu < kcu) & (bbl > kcl)      # BB inside KC

        start = len(combined) - len(today_5min)
        for i in range(max(start, 1), len(combined)):
            c = combined.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            if pd.isna(squeeze_on.iloc[i]) or pd.isna(atr.iloc[i]):
                continue
            # release = squeeze was ON on the prior bar, OFF now
            if squeeze_on.iloc[i - 1] and not squeeze_on.iloc[i]:
                a = float(atr.iloc[i])
                entry = float(c["close"])
                if c["close"] > mid.iloc[i]:                 # upward release
                    return self._buy(entry, entry + 2 * a, entry - a,
                                     signal_time=self._candle_time(c["datetime"]),
                                     reason="TTM-SQUEEZE: upward release")
                if c["close"] < mid.iloc[i]:                 # downward release
                    return self._sell(entry, entry - 2 * a, entry + a,
                                      signal_time=self._candle_time(c["datetime"]),
                                      reason="TTM-SQUEEZE: downward release")
        return self._no_signal()
