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

        bull_gap = None   # (gap_lo, gap_hi) of the most recent unfilled bullish FVG
        bear_gap = None   # (gap_lo, gap_hi) of the most recent unfilled bearish FVG

        for i in range(2, len(t)):
            c = t.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            price = float(c["close"])
            if price <= 0:
                continue
            lo, hi = float(c["low"]), float(c["high"])

            # check retrace-into-gap-and-resume on a PENDING gap before looking for a new
            # one -- the bar that fills the gap and holds is what triggers entry, not the
            # bar that formed the gap (chasing the gap print itself is what the old code did)
            if bull_gap is not None:
                gap_lo, gap_hi = bull_gap
                if hi < gap_lo:
                    bull_gap = None   # fully broke below without reacting -- invalidated
                elif lo <= gap_hi and c["close"] > gap_lo:
                    entry = price
                    stop = gap_lo
                    risk = entry - stop
                    if risk > 0:
                        return self._buy(entry, entry + 1.8 * risk, stop,
                                         signal_time=self._candle_time(c["datetime"]),
                                         reason=f"FVG: bullish retrace into {gap_lo:.2f}-{gap_hi:.2f}, resumed")
                    bull_gap = None

            if bear_gap is not None:
                gap_lo2, gap_hi2 = bear_gap
                if lo > gap_hi2:
                    bear_gap = None   # fully broke above without reacting -- invalidated
                elif hi >= gap_lo2 and c["close"] < gap_hi2:
                    entry = price
                    stop = gap_hi2
                    risk = stop - entry
                    if risk > 0:
                        return self._sell(entry, entry - 1.8 * risk, stop,
                                          signal_time=self._candle_time(c["datetime"]),
                                          reason=f"FVG: bearish retrace into {gap_lo2:.2f}-{gap_hi2:.2f}, resumed")
                    bear_gap = None

            # detect a new gap at this bar (candle1.high/low vs this bar's low/high);
            # the newest gap replaces any older, still-unfilled one of the same direction
            c1 = t.iloc[i - 2]
            gap_lo, gap_hi = float(c1["high"]), float(t.iloc[i]["low"])
            if gap_hi > gap_lo and (gap_hi - gap_lo) / price > self.MIN_GAP_FRAC:
                bull_gap = (gap_lo, gap_hi)

            gap_hi2, gap_lo2 = float(c1["low"]), float(t.iloc[i]["high"])
            if gap_hi2 > gap_lo2 and (gap_hi2 - gap_lo2) / price > self.MIN_GAP_FRAC:
                bear_gap = (gap_lo2, gap_hi2)

        return self._no_signal()
