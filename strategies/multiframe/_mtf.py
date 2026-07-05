"""Shared 15-min resample for the multi-timeframe strategies (B4d). Derived from the
existing 5-min Parquet — no new download. 15-min is independent information from 5-min."""
from __future__ import annotations

import pandas as pd


def to_15m(df_5min: pd.DataFrame) -> pd.DataFrame:
    """Resample 5-min OHLCV to 15-min bars aligned to the 09:15 open."""
    if df_5min is None or df_5min.empty:
        return pd.DataFrame()
    d = (df_5min.set_index("datetime")
         .resample("15min", origin="start_day", label="left", closed="left")
         .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
              close=("close", "last"), volume=("volume", "sum"))
         .dropna(subset=["open"]))
    return d.reset_index()
