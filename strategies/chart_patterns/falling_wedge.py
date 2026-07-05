"""
Falling Wedge — bullish reversal / continuation.

Daily chart shows lower highs AND lower lows, but the slope of lows is
shallower than the slope of highs (converging downward channel).
Signal fires when today's intraday price closes above the projected
upper trendline.
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class FallingWedge(BaseStrategy):
    name     = "FALL-WEDGE"
    category = "chart_pattern"

    LOOKBACK      = 30    # daily bars
    MIN_CONVERGE  = 0.25  # wedge must have converged by at least 25%

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty:
            return self._no_signal()
        daily = self._daily(history_5min, self.LOOKBACK)
        if len(daily) < 12:
            return self._no_signal()

        pat = self._detect(daily)
        if pat is None:
            return self._no_signal()

        entry, sig_time = self._find_breakout(today_5min, pat["upper"])
        if entry is None:
            return self._no_signal()

        return self._buy(
            entry, pat["target"], pat["stop"], signal_time=sig_time,
            reason=f"Falling Wedge: broke above upper trendline {pat['upper']:.2f}",
        )

    # ── detection ─────────────────────────────────────────────────────────────

    def _detect(self, daily):
        n      = len(daily)
        x      = np.arange(n, dtype=float)
        highs  = daily["high"].values.astype(float)
        lows   = daily["low"].values.astype(float)
        closes = daily["close"].values.astype(float)

        h_slope, h_int = np.polyfit(x, highs, 1)
        l_slope, l_int = np.polyfit(x, lows,  1)

        if h_slope >= 0 or l_slope >= 0:
            return None                        # both must be falling
        if l_slope <= h_slope:
            return None                        # lows must fall more slowly than highs

        spread_start = (h_int) - (l_int)
        spread_end   = (h_slope * (n-1) + h_int) - (l_slope * (n-1) + l_int)
        if spread_end >= spread_start * (1 - self.MIN_CONVERGE):
            return None                        # not enough convergence

        upper = h_slope * n + h_int            # projected to "today" (next bar)
        lower = l_slope * n + l_int

        wedge_h = upper - lower
        if wedge_h <= 0:
            return None

        # Price must be in lower 70% of wedge (building compression, not already broken out)
        pos = (closes[-1] - lower) / wedge_h
        if pos > 0.70 or closes[-1] > upper:
            return None

        target = float(highs[0])               # measured move: start of wedge
        stop   = lower * 0.99

        return {"upper": round(upper, 2), "target": target, "stop": stop}

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
