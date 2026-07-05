"""B4b — KELTNER-REV (Cluster B): ATR-based channel reversion. Bollinger uses std-dev;
Keltner uses ATR, so it fires differently in trending markets. Price outside the channel
reverts toward the mid (EMA)."""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta
from strategies.base import BaseStrategy, Signal


class KeltnerReversion(BaseStrategy):
    name     = "KELTNER-REV"
    category = "reversion"
    LEN      = 20
    MULT     = 2.0

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        combined = pd.concat([history_5min.tail(150), today_5min]).reset_index(drop=True)
        if len(combined) < self.LEN + 5:
            return self._no_signal()
        mid = ta.ema(combined["close"], length=self.LEN)
        atr = ta.atr(combined["high"], combined["low"], combined["close"], length=self.LEN)
        if mid is None or atr is None:
            return self._no_signal()
        upper = mid + self.MULT * atr
        lower = mid - self.MULT * atr
        start = len(combined) - len(today_5min)

        for i in range(start, len(combined)):
            c = combined.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            if pd.isna(upper.iloc[i]) or pd.isna(mid.iloc[i]):
                continue
            price = float(c["close"])
            if price <= float(lower.iloc[i]):                # below channel -> revert up
                stop = price - float(atr.iloc[i])
                if price - stop > 0:
                    return self._buy(price, float(mid.iloc[i]), stop,
                                     signal_time=self._candle_time(c["datetime"]),
                                     reason=f"KELTNER-REV: below lower channel {lower.iloc[i]:.2f}")
            if price >= float(upper.iloc[i]):                # above channel -> revert down
                stop = price + float(atr.iloc[i])
                if stop - price > 0:
                    return self._sell(price, float(mid.iloc[i]), stop,
                                      signal_time=self._candle_time(c["datetime"]),
                                      reason=f"KELTNER-REV: above upper channel {upper.iloc[i]:.2f}")
        return self._no_signal()
