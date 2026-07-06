"""
BaseStrategy — parent class for all 22 strategies.

Every strategy:
  - Receives today's 5-min DataFrame and context (prev day OHLC, Nifty data)
  - Returns a standardised signal dict
  - Never looks at data beyond the current day (no look-ahead)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, time
import pandas as pd


NO_ENTRY_AFTER = time(14, 0)   # 2:00 PM IST


def daily_ohlcv(history_5min: pd.DataFrame) -> pd.DataFrame:
    """Full daily OHLCV bars derived from history_5min, computed once and cached on
    the DataFrame itself via .attrs.

    A dozen-plus strategies each independently ran the same groupby(...dt.date).agg(...)
    on the identical history_5min object (same stock, same day — the engine passes the
    SAME object to every strategy in that day's evaluation), redundantly recomputing an
    expensive aggregation over what can be ~2 years of 5-min bars, once per strategy.
    Caching on .attrs is safe here specifically because history_5min is a freshly
    filtered DataFrame created new for every (stock, day) pair — it is never reused
    across days, so there is no cross-day staleness risk.
    """
    cached = history_5min.attrs.get("_daily_ohlcv")
    if cached is not None:
        return cached
    daily = (history_5min.groupby(history_5min["datetime"].dt.date)
             .agg(open=("open", "first"), high=("high", "max"),
                  low=("low", "min"), close=("close", "last"),
                  volume=("volume", "sum")))
    history_5min.attrs["_daily_ohlcv"] = daily
    return daily


@dataclass
class Signal:
    strategy:    str
    direction:   int    # +1 buy, 0 none, -1 sell (logged only in long-only mode)
    entry:       float  = 0.0
    target:      float  = 0.0
    stop:        float  = 0.0
    rr:          float  = 0.0   # risk:reward ratio
    signal_time: str    = ""    # "09:30", "10:15" etc.
    reason:      str    = ""

    @property
    def is_valid(self) -> bool:
        return self.direction != 0 and self.entry > 0 and self.target > 0 and self.stop > 0

    def to_dict(self) -> dict:
        return {
            "strategy":    self.strategy,
            "direction":   self.direction,
            "entry":       self.entry,
            "target":      self.target,
            "stop":        self.stop,
            "rr":          self.rr,
            "signal_time": self.signal_time,
            "reason":      self.reason,
        }


class BaseStrategy:
    name:     str = "BASE"
    category: str = "base"

    def generate_signal(
        self,
        today_5min:   pd.DataFrame,   # 5-min candles for today only
        history_5min: pd.DataFrame,   # 5-min candles for all past days (not today)
        prev_day:     pd.Series,      # previous day OHLCV (daily)
        nifty_today:  pd.DataFrame,   # Nifty 50 5-min candles for today
        trade_date:   date,
    ) -> Signal:
        raise NotImplementedError

    # ── helpers ──────────────────────────────────────────────────────────────

    def _no_signal(self) -> Signal:
        return Signal(strategy=self.name, direction=0)

    def _buy(self, entry: float, target: float, stop: float,
             signal_time: str = "", reason: str = "") -> Signal:
        if entry <= 0 or stop >= entry or target <= entry:
            return self._no_signal()
        rr = (target - entry) / (entry - stop) if entry != stop else 0
        return Signal(self.name, +1, entry, target, stop, round(rr, 2), signal_time, reason)

    def _sell(self, entry: float, target: float, stop: float,
              signal_time: str = "", reason: str = "") -> Signal:
        if entry <= 0 or stop <= entry or target >= entry:
            return self._no_signal()
        rr = (entry - target) / (stop - entry) if entry != stop else 0
        return Signal(self.name, -1, entry, target, stop, round(rr, 2), signal_time, reason)

    @staticmethod
    def _compute_vwap(df: pd.DataFrame) -> pd.Series:
        """Intraday VWAP from market open — resets each day."""
        tp = (df["high"] + df["low"] + df["close"]) / 3
        vwap = (tp * df["volume"]).cumsum() / df["volume"].cumsum()
        return vwap

    @staticmethod
    def _candle_time(dt) -> str:
        return pd.Timestamp(dt).strftime("%H:%M")

    @staticmethod
    def _after_cutoff(dt) -> bool:
        t = pd.Timestamp(dt).time()
        return t >= NO_ENTRY_AFTER

