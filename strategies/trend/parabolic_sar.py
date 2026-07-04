"""B4b — PARABOLIC-SAR (Cluster C): trailing-stop dot flip marks the trend reversal point.
EMA-CROSS/SUPERTREND track the trend; SAR identifies the reversal bar specifically."""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta
from strategies.base import BaseStrategy, Signal


class ParabolicSAR(BaseStrategy):
    name     = "PARABOLIC-SAR"
    category = "trend"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        combined = pd.concat([history_5min.tail(120), today_5min]).reset_index(drop=True)
        if len(combined) < 20:
            return self._no_signal()
        ps = ta.psar(combined["high"], combined["low"], combined["close"])
        atr = ta.atr(combined["high"], combined["low"], combined["close"], length=14)
        if ps is None:
            return self._no_signal()
        rev_col = [c for c in ps.columns if "PSARr" in c][0]
        long_col = [c for c in ps.columns if "PSARl" in c][0]
        start = len(combined) - len(today_5min)

        for i in range(max(start, 1), len(combined)):
            c = combined.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            if ps[rev_col].iloc[i] != 1 or pd.isna(atr.iloc[i]):
                continue
            entry = float(c["close"])
            a = float(atr.iloc[i])
            flipped_long = not pd.isna(ps[long_col].iloc[i])   # SAR now below price = uptrend
            if flipped_long:
                return self._buy(entry, entry + 1.8 * a, entry - a,
                                 signal_time=self._candle_time(c["datetime"]),
                                 reason="PARABOLIC-SAR: flip to uptrend")
            return self._sell(entry, entry - 1.8 * a, entry + a,
                              signal_time=self._candle_time(c["datetime"]),
                              reason="PARABOLIC-SAR: flip to downtrend")
        return self._no_signal()
