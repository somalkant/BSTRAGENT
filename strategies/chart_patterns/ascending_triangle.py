"""
Ascending Triangle — bullish continuation.

Flat resistance (multiple highs at roughly the same level) plus rising
lows on the daily chart. Signal fires when today's intraday price closes
above the resistance level.
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class AscendingTriangle(BaseStrategy):
    name     = "ASC-TRI"
    category = "chart_pattern"

    LOOKBACK    = 30
    FLAT_THRESH = 0.015   # resistance highs must be within 1.5% of each other
    MIN_PEAKS   = 2       # need at least 2 touches of resistance

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty:
            return self._no_signal()
        daily = self._daily(history_5min, self.LOOKBACK)
        if len(daily) < 12:
            return self._no_signal()

        pat = self._detect(daily)
        if pat is None:
            return self._no_signal()

        entry, sig_time = self._find_breakout(today_5min, pat["resistance"])
        if entry is None:
            return self._no_signal()

        return self._buy(
            entry, pat["target"], pat["stop"], signal_time=sig_time,
            reason=f"Ascending Triangle: breakout above resistance {pat['resistance']:.2f}",
        )

    # ── detection ─────────────────────────────────────────────────────────────

    def _detect(self, daily):
        n      = len(daily)
        highs  = daily["high"].values.astype(float)
        lows   = daily["low"].values.astype(float)
        closes = daily["close"].values.astype(float)

        # Local peaks (higher than immediate neighbours)
        peaks = [highs[i] for i in range(1, n - 1)
                 if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]]
        if len(peaks) < self.MIN_PEAKS:
            return None

        resistance = float(np.mean(peaks[-4:]))   # average of recent peaks

        # All recent peaks must cluster near resistance
        for p in peaks[-4:]:
            if abs(p - resistance) / resistance > self.FLAT_THRESH:
                return None

        # Lows must be rising
        x = np.arange(n, dtype=float)
        l_slope, _ = np.polyfit(x, lows, 1)
        if l_slope <= 0:
            return None

        # Price must be approaching but not already above resistance
        if closes[-1] > resistance * 1.02:
            return None
        if closes[-1] < resistance * 0.90:
            return None

        height = resistance - float(lows.min())
        target = resistance + height
        stop   = float(daily["low"].tail(5).min()) * 0.99

        return {"resistance": round(resistance, 2), "target": target, "stop": stop}

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
