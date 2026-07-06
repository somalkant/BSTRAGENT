"""
Bear Flag — bearish continuation.

Sharp impulsive drop (pole) of ≥8% in 3-7 days, followed by a mild
consolidation / slight upward bounce (the flag) on declining volume.
Signal fires when today's intraday price closes below the flag support.
Target = flag bottom - pole height (measured move).
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class BearFlag(BaseStrategy):
    name     = "BEAR-FLAG"
    category = "bearish"

    POLE_DAYS    = 7
    FLAG_DAYS    = 10
    MIN_POLE_PCT = 0.08   # pole must drop ≥8%
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

        entry, sig_time = self._find_breakdown(today_5min, pat["flag_bottom"])
        if entry is None:
            return self._no_signal()

        return self._sell(
            entry, pat["target"], pat["stop"], signal_time=sig_time,
            reason=f"Bear Flag: pole -{pat['pole_pct']:.1f}%, breakdown below {pat['flag_bottom']:.2f}",
        )

    def _detect(self, daily):
        n    = len(daily)
        pole = daily.iloc[n - self.FLAG_DAYS - self.POLE_DAYS : n - self.FLAG_DAYS]
        flag = daily.iloc[n - self.FLAG_DAYS :]

        pole_start = float(pole["close"].iloc[0])
        pole_bot   = float(pole["low"].min())
        pole_pct   = (pole_start - pole_bot) / pole_start

        if pole_pct < self.MIN_POLE_PCT:
            return None

        # Flag: mild consolidation — slope of lows must be flat or slightly up (pullback)
        fl = flag["low"].values.astype(float)
        fh = flag["high"].values.astype(float)
        fl_slope, _ = np.polyfit(range(len(fl)), fl, 1)

        # Flag slope must not be strongly downward (that would be continuation, not flag)
        if fl_slope < -(pole_pct * pole_start / self.POLE_DAYS):
            return None

        # Retrace check: flag high must not retrace more than 50% of pole
        flag_high  = float(flag["high"].max())
        retrace    = (flag_high - pole_bot) / (pole_start - pole_bot)
        if retrace > self.MAX_RETRACE:
            return None

        flag_bottom = float(flag["low"].min())
        pole_height = pole_start - pole_bot
        target      = flag_bottom - pole_height
        stop        = flag_high * 1.01

        return {
            "flag_bottom": round(flag_bottom, 2),
            "target":      target,
            "stop":        stop,
            "pole_pct":    pole_pct * 100,
        }

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
