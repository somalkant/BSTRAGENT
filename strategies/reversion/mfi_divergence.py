"""B4b — MFI-DIV (Cluster B): Money Flow Index extreme + divergence. RSI-EXT uses price
only; MFI incorporates volume (volume-weighted RSI)."""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta
from strategies.base import BaseStrategy, Signal
from strategies.reversion._divergence import find_divergence


class MFIDivergence(BaseStrategy):
    name     = "MFI-DIV"
    category = "reversion"
    MFI_LEN  = 14

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        combined = pd.concat([history_5min.tail(120), today_5min]).reset_index(drop=True)
        if len(combined) < self.MFI_LEN + 25:
            return self._no_signal()
        mfi = ta.mfi(combined["high"], combined["low"], combined["close"], combined["volume"],
                     length=self.MFI_LEN)
        start = len(combined) - len(today_5min)

        for i in range(start, len(combined)):
            c = combined.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            m = mfi.iloc[i]
            if pd.isna(m):
                continue
            d = find_divergence(combined["close"], mfi, i)
            entry = float(c["close"])
            atr = float((combined["high"] - combined["low"]).iloc[max(0, i - 14):i].mean())
            if atr <= 0:
                continue
            # divergence, or plain MFI extreme (overbought/oversold)
            if d == +1 or m < 20:
                return self._buy(entry, entry + 1.8 * atr, entry - atr,
                                 signal_time=self._candle_time(c["datetime"]),
                                 reason=f"MFI-DIV: {'bullish div' if d==1 else 'oversold'} MFI={m:.0f}")
            if d == -1 or m > 80:
                return self._sell(entry, entry - 1.8 * atr, entry + atr,
                                  signal_time=self._candle_time(c["datetime"]),
                                  reason=f"MFI-DIV: {'bearish div' if d==-1 else 'overbought'} MFI={m:.0f}")
        return self._no_signal()
