"""
Daily Trend Bias — multi-timeframe context signal.

Aggregates history_5min into daily OHLCV and checks 4 conditions:
  1. Price > 20-day MA       (in uptrend)
  2. 20-day MA slope rising  (trend strengthening)
  3. Daily RSI-14 > 50       (momentum positive)
  4. Price > 50-day MA       (medium-term trend confirmed)

Returns:
  direction = +1  if 3+ conditions are bullish  (boosts composite score)
  direction = -1  if 3+ conditions are bearish  (drags composite score down)
  direction =  0  if mixed                       (no vote)

This strategy never drives a trade directly (no entry/target/stop).
It acts as a vote in the composite scorer — stocks in daily uptrends
get a weight-sized boost; stocks in downtrends get suppressed.
"""
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, daily_ohlcv


class DailyTrendBias(BaseStrategy):
    name     = "DAILY-BIAS"
    category = "multi_timeframe"

    LOOKBACK     = 55    # daily bars (~50-day MA needs 50 + a few extra)
    MIN_DAYS     = 22    # minimum history before firing (one trading month)
    THRESHOLD    = 3     # conditions needed out of 4

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        if history_5min.empty:
            return self._no_signal()

        daily = self._daily(history_5min, self.LOOKBACK)
        if len(daily) < self.MIN_DAYS:
            return self._no_signal()

        closes = daily["close"].values.astype(float)
        current = closes[-1]

        # ── 4 daily conditions ────────────────────────────────────────────────
        ma20       = float(np.mean(closes[-20:]))
        ma20_prev  = float(np.mean(closes[-21:-1])) if len(closes) >= 21 else ma20
        ma50       = float(np.mean(closes[-50:])) if len(closes) >= 50 else float(np.mean(closes))
        rsi        = self._rsi(closes, period=14)

        bullish = [
            current > ma20,          # price above 20-day MA
            ma20 > ma20_prev,        # 20-day MA slope is rising
            rsi > 50,                # daily momentum positive
            current > ma50,          # price above 50-day MA
        ]
        bearish = [not c for c in bullish]

        bull_count = sum(bullish)
        bear_count = sum(bearish)

        if bull_count >= self.THRESHOLD:
            return Signal(
                strategy=self.name, direction=+1,
                reason=f"Daily uptrend {bull_count}/4: MA20={ma20:.0f} RSI={rsi:.0f}",
            )
        if bear_count >= self.THRESHOLD:
            return Signal(
                strategy=self.name, direction=-1,
                reason=f"Daily downtrend {bear_count}/4: MA20={ma20:.0f} RSI={rsi:.0f}",
            )
        return self._no_signal()

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _daily(history_5min, n):
        return daily_ohlcv(history_5min)[["close"]].tail(n)

    @staticmethod
    def _rsi(closes, period=14):
        if len(closes) < period + 1:
            return 50.0
        deltas    = np.diff(closes)
        gains     = np.where(deltas > 0, deltas, 0.0)[-period:]
        losses    = np.where(deltas < 0, -deltas, 0.0)[-period:]
        avg_gain  = float(np.mean(gains))
        avg_loss  = float(np.mean(losses))
        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
