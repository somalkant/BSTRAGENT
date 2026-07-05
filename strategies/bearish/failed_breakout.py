"""
Failed Breakout (Bull Trap) — the single most reliable intraday short signal.

Price breaks above a key resistance (PDH, ORB high) on volume, triggering
buy signals — but then closes back below the resistance within 1-2 bars on
above-average volume. Trapped longs become forced sellers.

Logic: identify a resistance level from history; wait for price to spike
above it then close below it with volume confirmation.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class FailedBreakout(BaseStrategy):
    name     = "FAILED-BO"
    category = "bearish"

    VOL_MULT     = 1.3    # reversal bar needs 1.3× average volume
    MAX_BARS_UP  = 2      # how many bars above resistance before reversal counts as "failed"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty or today_5min.empty or len(today_5min) < 4:
            return self._no_signal()

        resistance = self._find_resistance(history_5min, today_5min)
        if resistance is None:
            return self._no_signal()

        support = self._find_support(history_5min, today_5min)
        avg_vol = float(today_5min["volume"].mean()) or 1.0

        bars_above = 0

        for idx, c in today_5min.iterrows():
            if self._after_cutoff(c["datetime"]):
                break

            close = float(c["close"])
            high  = float(c["high"])
            vol   = float(c["volume"])

            if high > resistance:
                bars_above += 1

            # Reversal: was above resistance, now closed back below with volume
            if bars_above >= 1 and bars_above <= self.MAX_BARS_UP and close < resistance and vol > avg_vol * self.VOL_MULT:
                entry   = close
                stop    = high * 1.005      # just above the failed breakout high
                target  = support if support and support < entry else entry * 0.97

                return self._sell(
                    entry, target, stop,
                    signal_time=self._candle_time(c["datetime"]),
                    reason=f"Failed BO: bull trap above {resistance:.2f}, reversed on vol {vol/avg_vol:.1f}×",
                )

            if close > resistance:
                bars_above = max(bars_above, 1)
            elif close < resistance * 0.99:
                bars_above = 0   # reset if price dropped far below resistance

        return self._no_signal()

    def _find_resistance(self, history_5min, today_5min) -> float | None:
        # PDH (previous day high) is the most reliable intraday resistance
        if history_5min.empty:
            return None
        daily = daily_ohlcv(history_5min)
        if daily.empty:
            return None
        pdh = float(daily["high"].iloc[-1])
        # Also check ORB high (first 30-min range)
        orb_bars = today_5min[today_5min["datetime"].dt.time <= pd.Timestamp("09:45").time()]
        orb_high = float(orb_bars["high"].max()) if not orb_bars.empty else 0.0
        # Use whichever resistance level today's price is closest to from below
        open_p = float(today_5min.iloc[0]["open"])
        candidates = [r for r in [pdh, orb_high] if r > open_p * 0.995]
        return float(min(candidates, key=lambda r: r - open_p)) if candidates else None

    def _find_support(self, history_5min, today_5min) -> float | None:
        if history_5min.empty:
            return None
        daily = daily_ohlcv(history_5min)
        if daily.empty:
            return None
        return float(daily["low"].iloc[-1])   # PDL as nearest support
