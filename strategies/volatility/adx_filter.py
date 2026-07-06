"""
Strategy 18: ADX Filter (overlay, not standalone)
Returns a regime signal used as a weight modifier by the engine.
direction: +1 = trending (favour breakouts), -1 = sideways (favour reversions), 0 = neutral

Walks forward bar-by-bar and stamps signal_time on the first bar where the
condition holds, mirroring VWAP-STDDEV/CPR/CAMARILLA. pandas_ta's ADX is a
rolling/EMA-based calc — adx_df.iloc[i] already depends only on rows <= i —
but reading only adx_df.iloc[-1] (the last bar of the WHOLE day) reports the
end-of-day trend state regardless of when this vote is used to confirm a
trade, which is lookahead for any driver that fires earlier in the day.
"""
import pandas as pd
import pandas_ta as ta
from strategies.base import BaseStrategy, Signal


class ADXFilter(BaseStrategy):
    name     = "ADX-FILTER"
    category = "volatility"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date) -> Signal:
        combined = pd.concat([history_5min.tail(100), today_5min]).reset_index(drop=True)
        if len(combined) < 20:
            return self._no_signal()

        adx_df = ta.adx(combined["high"], combined["low"], combined["close"], length=14)
        if adx_df is None:
            return self._no_signal()

        adx_col = [c for c in adx_df.columns if c.startswith("ADX_")][0]
        dmp_col = [c for c in adx_df.columns if "DMP" in c][0]
        dmn_col = [c for c in adx_df.columns if "DMN" in c][0]

        today_start = len(combined) - len(today_5min)
        for i in range(today_start, len(combined)):
            c = combined.iloc[i]
            if self._after_cutoff(c["datetime"]):
                break
            row = adx_df.iloc[i]
            adx_val, dmp, dmn = row[adx_col], row[dmp_col], row[dmn_col]
            if pd.isna(adx_val):
                continue

            # ADX > 25 + DI+ > DI- → strong trend → direction +1
            if adx_val > 25 and dmp > dmn:
                return Signal(strategy=self.name, direction=+1,
                              signal_time=self._candle_time(c["datetime"]),
                              reason=f"ADX-FILTER: ADX={adx_val:.1f} trending, DI+={dmp:.1f}>DI-={dmn:.1f}")
            # ADX < 20 → sideways → direction -1
            if adx_val < 20:
                return Signal(strategy=self.name, direction=-1,
                              signal_time=self._candle_time(c["datetime"]),
                              reason=f"ADX-FILTER: ADX={adx_val:.1f} sideways")
        return self._no_signal()
