"""
Bull Flag — bullish continuation.

Strong upward pole (3-7 days, ≥8% gain) followed by a mild pullback /
sideways consolidation (the flag, 5-10 days, retracing ≤50% of pole).
Signal fires when today's intraday price closes above the flag's upper
boundary.  Target = flag top + pole height (measured move).
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class BullFlag(BaseStrategy):
    name     = "BULL-FLAG"
    category = "chart_pattern"

    POLE_DAYS    = 7
    FLAG_DAYS    = 10
    MIN_POLE_PCT = 0.08   # pole must gain ≥8%
    MAX_RETRACE  = 0.50   # flag must not retrace more than 50% of pole

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty:
            return self._no_signal()
        need = self.POLE_DAYS + self.FLAG_DAYS + 2
        daily = self._daily(history_5min, need)
        if len(daily) < self.POLE_DAYS + self.FLAG_DAYS:
            return self._no_signal()

        pat = self._detect(daily)
        if pat is None:
            return self._no_signal()

        entry, sig_time = self._find_breakout(today_5min, pat["flag_top"])
        if entry is None:
            return self._no_signal()

        return self._buy(
            entry, pat["target"], pat["stop"], signal_time=sig_time,
            reason=f"Bull Flag: pole +{pat['pole_pct']:.1f}%, breakout above {pat['flag_top']:.2f}",
        )

    # ── detection ─────────────────────────────────────────────────────────────

    def _detect(self, daily):
        n    = len(daily)
        pole = daily.iloc[n - self.FLAG_DAYS - self.POLE_DAYS : n - self.FLAG_DAYS]
        flag = daily.iloc[n - self.FLAG_DAYS :]

        pole_start = float(pole["close"].iloc[0])
        pole_top   = float(pole["high"].max())
        pole_pct   = (pole_top - pole_start) / pole_start

        if pole_pct < self.MIN_POLE_PCT:
            return None

        # Flag: mild consolidation — slope of highs must be flat or slightly down
        fh = flag["high"].values.astype(float)
        fl = flag["low"].values.astype(float)
        fh_slope, _ = np.polyfit(range(len(fh)), fh, 1)

        # Flag slope must not be strongly upward (that would be continuation, not flag)
        if fh_slope > pole_pct * pole_start / self.POLE_DAYS:
            return None

        # Retrace check: flag low must not give back >50% of pole
        flag_low   = float(flag["low"].min())
        retrace    = (pole_top - flag_low) / (pole_top - pole_start)
        if retrace > self.MAX_RETRACE:
            return None

        flag_top    = float(flag["high"].max())
        pole_height = pole_top - pole_start
        target      = flag_top + pole_height
        stop        = flag_low * 0.99

        return {
            "flag_top":  round(flag_top, 2),
            "target":    target,
            "stop":      stop,
            "pole_pct":  pole_pct * 100,
        }

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
