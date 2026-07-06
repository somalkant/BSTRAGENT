"""
Descending Triangle — bearish continuation.

Lower highs (resistance falling) plus flat support (multiple lows at
roughly the same level). Signal fires when today's intraday price closes
below the support level.
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class DescendingTriangle(BaseStrategy):
    name     = "DESC-TRI"
    category = "bearish"

    LOOKBACK    = 30
    FLAT_THRESH = 0.015   # support lows must be within 1.5% of each other
    MIN_TROUGHS = 2       # need at least 2 touches of support

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty:
            return self._no_signal()
        daily = self._daily(history_5min, self.LOOKBACK)
        if len(daily) < 12:
            return self._no_signal()

        pat = self._detect(daily)
        if pat is None:
            return self._no_signal()

        entry, sig_time = self._find_breakdown(today_5min, pat["support"])
        if entry is None:
            return self._no_signal()

        return self._sell(
            entry, pat["target"], pat["stop"], signal_time=sig_time,
            reason=f"Descending Triangle: breakdown below support {pat['support']:.2f}",
        )

    def _detect(self, daily):
        n      = len(daily)
        highs  = daily["high"].values.astype(float)
        lows   = daily["low"].values.astype(float)
        closes = daily["close"].values.astype(float)

        troughs = [lows[i] for i in range(1, n - 1)
                   if lows[i] <= lows[i-1] and lows[i] <= lows[i+1]]
        if len(troughs) < self.MIN_TROUGHS:
            return None

        support = float(np.mean(troughs[-4:]))

        for t in troughs[-4:]:
            if abs(t - support) / support > self.FLAT_THRESH:
                return None

        # Highs must be falling (lower highs)
        x = np.arange(n, dtype=float)
        h_slope, _ = np.polyfit(x, highs, 1)
        if h_slope >= 0:
            return None

        # Price must be approaching support but not already below it
        if closes[-1] < support * 0.98:
            return None
        if closes[-1] > support * 1.10:
            return None

        height = float(highs.max()) - support
        target = support - height
        stop   = float(daily["high"].tail(5).max()) * 1.01

        return {"support": round(support, 2), "target": target, "stop": stop}

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
