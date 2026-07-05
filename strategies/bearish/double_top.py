"""
Double Top (M-pattern) — bearish reversal.

Two peaks at similar price levels on the daily chart, separated by a
trough (neckline). Signal fires when today's intraday price closes below
the neckline for the first time.
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class DoubleTop(BaseStrategy):
    name     = "DBL-TOP"
    category = "bearish"

    LOOKBACK   = 60
    TOLERANCE  = 0.03   # two tops must be within 3% of each other
    MIN_SEP    = 5      # minimum trading days between the two peaks
    MAX_AGE    = 15     # second peak must be within last 15 days

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty:
            return self._no_signal()
        daily = self._daily(history_5min, self.LOOKBACK)
        if len(daily) < 20:
            return self._no_signal()

        pat = self._detect(daily)
        if pat is None:
            return self._no_signal()

        entry, sig_time = self._find_breakdown(today_5min, pat["neckline"])
        if entry is None:
            return self._no_signal()

        return self._sell(
            entry, pat["target"], pat["stop"], signal_time=sig_time,
            reason=f"Double Top: neckline={pat['neckline']:.2f} top={pat['top']:.2f}",
        )

    def _detect(self, daily):
        highs  = daily["high"].values
        lows   = daily["low"].values
        closes = daily["close"].values
        n      = len(highs)

        peaks = [
            (i, highs[i]) for i in range(3, n - 2)
            if highs[i] >= highs[i-1] and highs[i] >= highs[i-2]
            and highs[i] >= highs[i+1] and highs[i] >= highs[i+2]
        ]
        if len(peaks) < 2:
            return None

        t1_idx, t1 = peaks[-2]
        t2_idx, t2 = peaks[-1]

        if t2_idx - t1_idx < self.MIN_SEP:
            return None
        if n - 1 - t2_idx > self.MAX_AGE:
            return None
        avg = (t1 + t2) / 2
        if abs(t1 - t2) / avg > self.TOLERANCE:
            return None

        neckline = float(daily["low"].iloc[t1_idx : t2_idx + 1].min())
        top      = max(t1, t2)
        target   = neckline - (top - neckline)
        stop     = top * 1.015

        # Price must be approaching neckline from above (not already far below)
        if closes[-1] < neckline * 0.96:
            return None

        return {"neckline": neckline, "top": top, "target": target, "stop": stop}

    @staticmethod
    def _daily(history_5min, n):
        return daily_ohlcv(history_5min)[["open", "high", "low", "close"]].tail(n)

    def _find_breakdown(self, today_5min, level):
        for _, c in today_5min.iterrows():
            if self._after_cutoff(c["datetime"]):
                break
            if float(c["close"]) < level:
                return float(c["close"]), self._candle_time(c["datetime"])
        return None, None
