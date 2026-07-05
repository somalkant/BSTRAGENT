"""
Double Bottom (W-pattern) — bullish reversal.

Two troughs at similar price levels on the daily chart, separated by a
peak (neckline). Signal fires when today's intraday price closes above
the neckline for the first time.
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class DoubleBottom(BaseStrategy):
    name     = "DBL-BTM"
    category = "chart_pattern"

    LOOKBACK   = 60     # daily bars to scan
    TOLERANCE  = 0.03   # two bottoms must be within 3% of each other
    MIN_SEP    = 5      # minimum trading days between the two troughs
    MAX_AGE    = 15     # second trough must be within last 15 days

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty:
            return self._no_signal()
        daily = self._daily(history_5min, self.LOOKBACK)
        if len(daily) < 20:
            return self._no_signal()

        pat = self._detect(daily)
        if pat is None:
            return self._no_signal()

        entry, sig_time = self._find_breakout(today_5min, pat["neckline"])
        if entry is None:
            return self._no_signal()

        return self._buy(
            entry, pat["target"], pat["stop"], signal_time=sig_time,
            reason=f"Double Bottom: neckline={pat['neckline']:.2f} bottom={pat['bottom']:.2f}",
        )

    # ── detection ─────────────────────────────────────────────────────────────

    def _detect(self, daily):
        lows   = daily["low"].values
        highs  = daily["high"].values
        closes = daily["close"].values
        n      = len(lows)

        troughs = [
            (i, lows[i]) for i in range(3, n - 2)
            if lows[i] <= lows[i-1] and lows[i] <= lows[i-2]
            and lows[i] <= lows[i+1] and lows[i] <= lows[i+2]
        ]
        if len(troughs) < 2:
            return None

        t1_idx, t1 = troughs[-2]
        t2_idx, t2 = troughs[-1]

        if t2_idx - t1_idx < self.MIN_SEP:
            return None
        if n - 1 - t2_idx > self.MAX_AGE:
            return None
        avg = (t1 + t2) / 2
        if abs(t1 - t2) / avg > self.TOLERANCE:
            return None

        neckline = float(daily["high"].iloc[t1_idx : t2_idx + 1].max())
        bottom   = min(t1, t2)
        target   = neckline + (neckline - bottom)
        stop     = bottom * 0.985

        # price must be approaching neckline (not already far above it)
        if closes[-1] > neckline * 1.04:
            return None

        return {"neckline": neckline, "bottom": bottom, "target": target, "stop": stop}

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _daily(history_5min, n):
        return daily_ohlcv(history_5min)[["open", "high", "low", "close"]].tail(n)

    def _find_breakout(self, today_5min, level):
        for _, c in today_5min.iterrows():
            if self._after_cutoff(c["datetime"]):
                break
            if float(c["close"]) > level:
                return float(c["close"]), self._candle_time(c["datetime"])
        return None, None
