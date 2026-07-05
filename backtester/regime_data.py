"""
Per-day regime inputs (plan_phase2.md §1) — VIX + Nifty daily ADX/return, lookahead-free.

Builds, for each trading date t, the scalars the RegimeClassifier needs:
  vix        : India VIX close on t-1 (known at t's open — lookahead-free)
  adx        : ADX(14) of daily Nifty through t-1
  nifty_ret  : Nifty day-return on t (for R4 crash; used for the trade's regime tag)
  vix_bands  : trailing-252d P75/P80/P85 of VIX closes from data <= t-1
"""
from __future__ import annotations

import pandas as pd

from config.settings import INDEX_DIR
from weights.regime import vix_thresholds
import pandas_ta as ta


def _load_index(symbol: str, years) -> pd.DataFrame | None:
    frames = []
    for y in years:
        f = INDEX_DIR / str(y) / f"{symbol}.parquet"
        if f.exists():
            df = pd.read_parquet(f)
            df["datetime"] = pd.to_datetime(df["datetime"])
            frames.append(df)
    if not frames:
        return None
    return pd.concat(frames).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)


def build_regime_inputs(year: int) -> dict:
    """Return {date: {vix, adx, nifty_ret, vix_bands}} for `year` (uses prev year for warmup)."""
    years = [year - 2, year - 1, year]
    nifty = _load_index("NIFTY50", years)
    vix = _load_index("INDIAVIX", years)
    if nifty is None:
        return {}

    nd = nifty.groupby(nifty["datetime"].dt.date).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    nd["ret"] = (nd["close"] - nd["open"]) / nd["open"] * 100.0
    adx_df = ta.adx(nd["high"], nd["low"], nd["close"], length=14)
    nd["adx"] = adx_df[[c for c in adx_df.columns if c.startswith("ADX_")][0]].to_numpy() \
        if adx_df is not None else 20.0

    vix_daily = None
    if vix is not None:
        vix_daily = vix.groupby(vix["datetime"].dt.date)["close"].last()

    dates = [d for d in nd.index if d.year == year]
    out = {}
    all_dates = list(nd.index)
    for d in dates:
        i = all_dates.index(d)
        prev = all_dates[i - 1] if i > 0 else d
        vix_close = float(vix_daily.get(prev, 15.0)) if vix_daily is not None else 15.0
        adx = float(nd.loc[prev, "adx"]) if not pd.isna(nd.loc[prev, "adx"]) else 20.0
        # trailing VIX closes up to t-1 (exclusive of t)
        bands = None
        if vix_daily is not None:
            trailing = vix_daily[vix_daily.index < d]
            bands = vix_thresholds(trailing.tolist())
        out[d] = {
            "vix": vix_close, "adx": adx, "nifty_ret": float(nd.loc[d, "ret"]),
            "vix_bands": bands,
        }
    return out
