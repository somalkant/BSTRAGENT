"""Strategy 5: Volume Spike Breakout — price at resistance + volume > 2× 20-day avg."""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class VolumeSpikeBreakout(BaseStrategy):
    name      = "VOL-SPIKE"
    category  = "breakout"
    VOL_MULT  = 2.0
    LOOKBACK  = 20   # days for average volume

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty:
            return self._no_signal()

        # Daily avg volume from recent history
        daily_vol = daily_ohlcv(history_5min)["volume"].tail(self.LOOKBACK)
        if len(daily_vol) < 5:
            return self._no_signal()
        avg_daily_vol = daily_vol.mean()

        # 20-day resistance = highest high in last 20 trading days
        resistance = daily_ohlcv(history_5min)["high"].tail(self.LOOKBACK).max()

        today_vol_so_far = 0
        for _, c in today_5min.iterrows():
            if self._after_cutoff(c["datetime"]):
                break
            today_vol_so_far += c["volume"]
            vol_ratio = today_vol_so_far / (avg_daily_vol + 1)

            if c["close"] > resistance * 0.999 and vol_ratio > self.VOL_MULT:
                entry  = c["close"]
                stop   = resistance * 0.98
                target = entry + 2 * (entry - stop)
                return self._buy(entry, target, stop,
                                  signal_time=self._candle_time(c["datetime"]),
                                  reason=f"VOL-SPIKE: price at resistance {resistance:.2f}, vol {vol_ratio:.1f}x avg")
        return self._no_signal()
