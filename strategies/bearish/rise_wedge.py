"""
Rising Wedge — bearish reversal / continuation.

Daily chart shows higher highs AND higher lows, but the slope of highs
is shallower than the slope of lows (converging upward channel — lows
rising faster than highs). Breakout bias is downward.
Signal fires when today's intraday price closes below the lower trendline.
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class RisingWedge(BaseStrategy):
    name     = "RISE-WEDGE"
    category = "bearish"

    LOOKBACK     = 30
    MIN_CONVERGE = 0.25   # wedge must have converged by at least 25%

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty:
            return self._no_signal()
        daily = self._daily(history_5min, self.LOOKBACK)
        if len(daily) < 12:
            return self._no_signal()

        pat = self._detect(daily)
        if pat is None:
            return self._no_signal()

        entry, sig_time = self._find_breakdown(today_5min, pat["lower"])
        if entry is None:
            return self._no_signal()

        return self._sell(
            entry, pat["target"], pat["stop"], signal_time=sig_time,
            reason=f"Rising Wedge: broke below lower trendline {pat['lower']:.2f}",
        )

    def _detect(self, daily):
        n      = len(daily)
        x      = np.arange(n, dtype=float)
        highs  = daily["high"].values.astype(float)
        lows   = daily["low"].values.astype(float)
        closes = daily["close"].values.astype(float)

        h_slope, h_int = np.polyfit(x, highs, 1)
        l_slope, l_int = np.polyfit(x, lows,  1)

        # Both must be rising
        if h_slope <= 0 or l_slope <= 0:
            return None
        # Lows must rise faster than highs (converging from below)
        if l_slope <= h_slope:
            return None

        spread_start = (h_int) - (l_int)
        spread_end   = (h_slope * (n-1) + h_int) - (l_slope * (n-1) + l_int)
        if spread_end >= spread_start * (1 - self.MIN_CONVERGE):
            return None  # not enough convergence

        lower = l_slope * n + l_int   # projected lower trendline for "today"
        upper = h_slope * n + h_int

        wedge_h = upper - lower
        if wedge_h <= 0:
            return None

        # Price must be in upper 70% of wedge (near top, ready to break)
        pos = (closes[-1] - lower) / wedge_h
        if pos < 0.30 or closes[-1] < lower:
            return None

        target = float(lows[0])   # measured move back to wedge start
        stop   = upper * 1.01

        return {"lower": round(lower, 2), "target": target, "stop": stop}

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
