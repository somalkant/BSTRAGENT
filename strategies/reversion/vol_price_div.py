"""B4b — VOL-PRICE-DIV (Cluster B): price makes a new high while volume declines =
distribution (smart money selling into strength). VOL-SPIKE fires on HIGH volume; this
fires on DECLINING volume at a price extreme."""
from __future__ import annotations

import pandas as pd
from strategies.base import BaseStrategy, Signal


class VolumePriceDivergence(BaseStrategy):
    name     = "VOL-PRICE-DIV"
    category = "reversion"
    LOOKBACK = 12

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        t = today_5min.reset_index(drop=True)
        if len(t) < self.LOOKBACK + 3:
            return self._no_signal()

        vol_ma = t["volume"].rolling(self.LOOKBACK).mean()
        for i in range(self.LOOKBACK + 1, len(t)):
            c = t.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            prior = t.iloc[:i]
            price = float(c["close"])
            atr = float((prior["high"] - prior["low"]).tail(14).mean())
            if atr <= 0:
                continue
            new_high = price >= float(prior["high"].max())
            new_low = price <= float(prior["low"].min())
            vol_falling = c["volume"] < 0.7 * vol_ma.iloc[i] if not pd.isna(vol_ma.iloc[i]) else False

            if new_high and vol_falling:                 # distribution -> fade short
                return self._sell(price, price - 1.6 * atr, price + atr,
                                  signal_time=self._candle_time(c["datetime"]),
                                  reason="VOL-PRICE-DIV: new high on falling volume")
            if new_low and vol_falling:                  # selling exhaustion -> fade long
                return self._buy(price, price + 1.6 * atr, price - atr,
                                 signal_time=self._candle_time(c["datetime"]),
                                 reason="VOL-PRICE-DIV: new low on falling volume")
        return self._no_signal()
