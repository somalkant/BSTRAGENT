"""B4a — THREE-BAR-REV (Cluster D): confirmed 3-candle reversals — Morning Star / Three
White Soldiers (bullish) and Evening Star / Three Black Crows (bearish). PIN-BAR is a
single candle; these require a completed 3-candle structure."""
from __future__ import annotations

import pandas as pd
from strategies.base import BaseStrategy, Signal


def _body(o, c):
    return abs(c - o)


class ThreeBarReversal(BaseStrategy):
    name     = "THREE-BAR-REV"
    category = "structure"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        t = today_5min.reset_index(drop=True)
        if len(t) < 4:
            return self._no_signal()

        for i in range(2, len(t)):
            a, b, c = t.iloc[i - 2], t.iloc[i - 1], t.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            ao, ac = float(a["open"]), float(a["close"])
            bo, bc = float(b["open"]), float(b["close"])
            co, cc = float(c["open"]), float(c["close"])
            rng = float(c["high"]) - float(c["low"])
            if rng <= 0:
                continue

            # Morning Star: big down, small body, big up closing > mid of candle a
            morning_star = (ac < ao and _body(bo, bc) < _body(ao, ac) * 0.5 and
                            cc > co and cc > (ao + ac) / 2)
            three_soldiers = (ac > ao and bc > bo and cc > co and
                              cc > bc > ac and co > ao)
            if morning_star or three_soldiers:
                entry = cc
                stop = min(float(a["low"]), float(b["low"]), float(c["low"]))
                risk = entry - stop
                if risk > 0:
                    return self._buy(entry, entry + 1.8 * risk, stop,
                                     signal_time=self._candle_time(c["datetime"]),
                                     reason="THREE-BAR-REV: " +
                                            ("morning-star" if morning_star else "three-white-soldiers"))

            # Evening Star: big up, small body, big down closing < mid of candle a
            evening_star = (ac > ao and _body(bo, bc) < _body(ao, ac) * 0.5 and
                            cc < co and cc < (ao + ac) / 2)
            three_crows = (ac < ao and bc < bo and cc < co and
                           cc < bc < ac and co < ao)
            if evening_star or three_crows:
                entry = cc
                stop = max(float(a["high"]), float(b["high"]), float(c["high"]))
                risk = stop - entry
                if risk > 0:
                    return self._sell(entry, entry - 1.8 * risk, stop,
                                      signal_time=self._candle_time(c["datetime"]),
                                      reason="THREE-BAR-REV: " +
                                             ("evening-star" if evening_star else "three-black-crows"))
        return self._no_signal()
