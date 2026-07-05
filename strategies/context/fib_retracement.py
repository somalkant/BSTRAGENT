"""B4a — FIB-RETRACEMENT (Cluster E, Context — NOT driver-eligible): swing-based
38.2/50/61.8% pullback levels. CPR/Camarilla are fixed daily pivots; Fib levels are
dynamic, computed from the day's developing swing. Advisory vote only."""
from __future__ import annotations

import pandas as pd
from strategies.base import BaseStrategy, Signal


class FibRetracement(BaseStrategy):
    name     = "FIB-RETRACEMENT"
    category = "context"
    TOL      = 0.002    # within 0.2% of a fib level counts as a touch

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        t = today_5min.reset_index(drop=True)
        if len(t) < 8:
            return self._no_signal()

        for i in range(6, len(t)):
            c = t.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            window = t.iloc[:i]                       # data strictly before this bar
            swing_hi = float(window["high"].max())
            swing_lo = float(window["low"].min())
            span = swing_hi - swing_lo
            price = float(c["close"])
            if span <= 0 or price <= 0:
                continue

            up_leg = window["high"].idxmax() > window["low"].idxmin()   # low then high = up-swing
            for f in (0.382, 0.5, 0.618):
                if up_leg:
                    level = swing_hi - f * span                          # pullback in an up-swing
                    if abs(price - level) / price < self.TOL:
                        stop = swing_lo
                        risk = price - stop
                        if risk > 0:
                            return self._buy(price, price + 1.6 * risk, stop,
                                             signal_time=self._candle_time(c["datetime"]),
                                             reason=f"FIB: {f*100:.1f}% pullback in up-swing")
                else:
                    level = swing_lo + f * span                          # pullback in a down-swing
                    if abs(price - level) / price < self.TOL:
                        stop = swing_hi
                        risk = stop - price
                        if risk > 0:
                            return self._sell(price, price - 1.6 * risk, stop,
                                              signal_time=self._candle_time(c["datetime"]),
                                              reason=f"FIB: {f*100:.1f}% pullback in down-swing")
        return self._no_signal()
