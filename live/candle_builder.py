"""
Build 5-minute OHLCV candles from a stream of KiteTicker ticks.

One CandleBuilder instance per symbol.

Volume is computed as:
    bar_volume = cumulative_day_volume_at_bar_close
                 - cumulative_day_volume_at_bar_open
This uses Kite's `volume` field (total traded volume today), which is monotonically
increasing. Subtracting the snapshot at bar-open gives the correct per-bar volume.
"""

from __future__ import annotations

import threading
from datetime import datetime

import pandas as pd


class CandleBuilder:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self._lock: threading.Lock = threading.Lock()
        self._o: float | None = None   # bar open price
        self._h: float = 0.0
        self._l: float = float("inf")
        self._c: float = 0.0
        self._vol_start: int = 0       # cumulative volume when bar opened
        self._vol_now:   int = 0       # latest cumulative day volume
        self._vol_synced: bool = False # re-sync _vol_now on first tick (handles fresh start + mid-day restart)
        self._closed: list[dict] = []

    def seed(self, bars: list[dict]) -> None:
        """
        Pre-populate with bars saved from a previous run today (agent resume).
        Sets _vol_synced=False so the first new tick re-baselines volume correctly
        instead of computing a delta against 0.
        """
        with self._lock:
            self._closed = list(bars)
            self._vol_synced = False   # trigger re-baseline on first incoming tick

    @property
    def closed_bars(self) -> list[dict]:
        """Raw closed bar list for checkpointing (shallow copy)."""
        with self._lock:
            return list(self._closed)

    def on_tick(self, price: float, day_volume: int) -> None:
        """Update the open bar with a new tick (call from KiteTicker on_ticks)."""
        if price <= 0:
            return
        with self._lock:
            # After a resume (seed), sync _vol_now to actual cumulative before
            # setting _vol_start, so the resumed bar's volume delta is correct.
            if not self._vol_synced:
                self._vol_now    = day_volume
                self._vol_synced = True

            if self._o is None:
                # First tick of this bar — snapshot volume at bar open
                self._o         = price
                self._h         = price
                self._l         = price
                self._vol_start = self._vol_now  # volume before this bar started
            self._h        = max(self._h, price)
            self._l        = min(self._l, price)
            self._c        = price
            self._vol_now  = day_volume

    def close_bar(self, bar_dt: datetime) -> dict | None:
        """
        Finalise the current bar at bar_dt and reset for the next bar.
        Returns the OHLCV dict, or None if no ticks arrived this bar.
        Called every 5 minutes by the agent scheduler.
        """
        with self._lock:
            if self._o is None:
                return None   # no ticks this bar (stock halted / pre-open)

            bar_volume = max(0, self._vol_now - self._vol_start)
            row = {
                "datetime": bar_dt,
                "open":   self._o,
                "high":   self._h,
                "low":    self._l,
                "close":  self._c,
                "volume": bar_volume,
            }
            self._closed.append(row)

            # Reset for next bar; carry forward vol_now as the next bar's vol_start
            self._o         = None
            self._h         = 0.0
            self._l         = float("inf")
            self._c         = 0.0
            self._vol_start = self._vol_now
            return row

    @property
    def today_df(self) -> pd.DataFrame:
        """Today's closed 5-min candles as a DataFrame (matches parquet schema)."""
        with self._lock:
            closed_copy = list(self._closed)
        if not closed_copy:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(closed_copy)
        df["symbol"] = self.symbol
        return df

    @property
    def last_price(self) -> float | None:
        """Most recent tick price (for monitoring open positions)."""
        with self._lock:
            if self._o is not None:
                return self._c
            return self._closed[-1]["close"] if self._closed else None
