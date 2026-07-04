"""B4a — FVG: Fair Value Gap (Cluster D). A 3-candle institutional imbalance: for a
bullish FVG candle1.high < candle3.low (an unfilled gap). We enter in the gap direction
when price retraces INTO the gap zone and resumes — capturing the imbalance fill+continuation.
Nothing in the base 32 captures order-flow imbalance zones."""
from __future__ import annotations

import pandas as pd
from strategies.base import BaseStrategy, Signal


class FairValueGap(BaseStrategy):
    name     = "FVG"
    category = "structure"
    MIN_GAP_FRAC = 0.001    # gap must span > 0.1% of price to matter

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        t = today_5min.reset_index(drop=True)
        if len(t) < 4:
            return self._no_signal()

        for i in range(2, len(t)):
            c1, c3, c = t.iloc[i - 2], t.iloc[i], t.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            price = float(c["close"])
            if price <= 0:
                continue

            # bullish FVG: gap between candle1 high and candle3 low
            gap_lo, gap_hi = float(c1["high"]), float(c3["low"])
            if gap_hi > gap_lo and (gap_hi - gap_lo) / price > self.MIN_GAP_FRAC:
                # enter long on the close that forms the gap, riding the imbalance up
                entry = price
                stop = gap_lo                                 # gap base
                risk = entry - stop
                if risk > 0:
                    return self._buy(entry, entry + 1.8 * risk, stop,
                                     signal_time=self._candle_time(c["datetime"]),
                                     reason=f"FVG: bullish imbalance {gap_lo:.2f}-{gap_hi:.2f}")

            # bearish FVG: gap between candle1 low and candle3 high
            gap_hi2, gap_lo2 = float(c1["low"]), float(c3["high"])
            if gap_hi2 > gap_lo2 and (gap_hi2 - gap_lo2) / price > self.MIN_GAP_FRAC:
                entry = price
                stop = gap_hi2
                risk = stop - entry
                if risk > 0:
                    return self._sell(entry, entry - 1.8 * risk, stop,
                                      signal_time=self._candle_time(c["datetime"]),
                                      reason=f"FVG: bearish imbalance {gap_lo2:.2f}-{gap_hi2:.2f}")
        return self._no_signal()
