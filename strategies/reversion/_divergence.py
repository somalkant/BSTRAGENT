"""Shared divergence detection for RSI-DIV / MFI-DIV (B4b)."""
from __future__ import annotations

import pandas as pd


def find_divergence(price: pd.Series, osc: pd.Series, i: int, window: int = 20,
                    gap: int = 3) -> int:
    """
    Return +1 (bullish divergence), -1 (bearish divergence), or 0 at index i.
    Bearish: price makes a higher high than the prior swing high, oscillator a lower high.
    Bullish: price makes a lower low than the prior swing low, oscillator a higher low.
    Only uses data up to i (no lookahead).
    """
    lo = max(0, i - window)
    if i - lo < gap + 2:
        return 0
    seg_p = price.iloc[lo:i - gap]        # prior swing region, excludes the last `gap` bars
    seg_o = osc.iloc[lo:i - gap]
    if seg_p.isna().all() or pd.isna(osc.iloc[i]) or pd.isna(price.iloc[i]):
        return 0

    prev_hi_idx = seg_p.idxmax()
    prev_lo_idx = seg_p.idxmin()
    p_now, o_now = float(price.iloc[i]), float(osc.iloc[i])

    if p_now > float(price.loc[prev_hi_idx]) and o_now < float(osc.loc[prev_hi_idx]):
        return -1                          # bearish divergence
    if p_now < float(price.loc[prev_lo_idx]) and o_now > float(osc.loc[prev_lo_idx]):
        return +1                          # bullish divergence
    return 0
