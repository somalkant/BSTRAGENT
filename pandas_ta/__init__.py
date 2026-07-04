"""
Vendored `pandas_ta` shim — Phase 1 Bayesian rebuild.

The upstream `pandas_ta` package no longer ships a Python-3.9-compatible release
on PyPI (0.4.x requires Python >= 3.12; 0.3.14b0 was pulled). This module provides
faithful, textbook implementations of ONLY the seven indicators the strategy library
imports, with the exact column-name conventions the strategies rely on:

    rsi        -> Series
    ema        -> Series
    bbands     -> DataFrame  BBL_/BBM_/BBU_/BBB_/BBP_
    macd       -> DataFrame  MACD_/MACDh_/MACDs_
    stoch      -> DataFrame  STOCHk_/STOCHd_
    supertrend -> DataFrame  SUPERT_/SUPERTd_/SUPERTl_/SUPERTs_   (d = +1 up / -1 down)
    adx        -> DataFrame  ADX_/DMP_/DMN_

Wilder-smoothed indicators (rsi, adx, supertrend ATR) use RMA (ewm alpha=1/length,
adjust=False) exactly as pandas_ta does. These are consistent across train and test,
so no lookahead is introduced. If you later run Python >= 3.12 you can `pip install
pandas_ta` and delete this folder; the real package will take precedence only if this
shim is removed (repo root is first on sys.path).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["rsi", "ema", "bbands", "macd", "stoch", "supertrend", "adx"]


def _rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's moving average (RMA) — pandas_ta's default smoothing."""
    return series.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def ema(close: pd.Series, length: int = 10) -> pd.Series:
    close = pd.Series(close, dtype="float64")
    out = close.ewm(span=length, min_periods=length, adjust=False).mean()
    out.name = f"EMA_{length}"
    return out


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    close = pd.Series(close, dtype="float64")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _rma(gain, length)
    avg_loss = _rma(loss, length)
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0, 100.0)   # all-gains window -> RSI 100
    out.name = f"RSI_{length}"
    return out


def bbands(close: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame | None:
    close = pd.Series(close, dtype="float64")
    if len(close) < length:
        return None
    mid = close.rolling(length).mean()
    sd = close.rolling(length).std(ddof=0)
    lower = mid - std * sd
    upper = mid + std * sd
    bandwidth = (upper - lower) / mid * 100.0
    percent = (close - lower) / (upper - lower)
    s = f"{length}_{std}"
    return pd.DataFrame({
        f"BBL_{s}": lower,
        f"BBM_{s}": mid,
        f"BBU_{s}": upper,
        f"BBB_{s}": bandwidth,
        f"BBP_{s}": percent,
    }, index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame | None:
    close = pd.Series(close, dtype="float64")
    if len(close) < slow:
        return None
    fast_ema = close.ewm(span=fast, min_periods=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, min_periods=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, min_periods=signal, adjust=False).mean()
    hist = macd_line - signal_line
    s = f"{fast}_{slow}_{signal}"
    return pd.DataFrame({
        f"MACD_{s}": macd_line,
        f"MACDh_{s}": hist,
        f"MACDs_{s}": signal_line,
    }, index=close.index)


def stoch(high: pd.Series, low: pd.Series, close: pd.Series,
          k: int = 14, d: int = 3, smooth_k: int = 3) -> pd.DataFrame | None:
    high = pd.Series(high, dtype="float64")
    low = pd.Series(low, dtype="float64")
    close = pd.Series(close, dtype="float64")
    if len(close) < k + smooth_k:
        return None
    lowest = low.rolling(k).min()
    highest = high.rolling(k).max()
    rng = (highest - lowest).replace(0, np.nan)
    raw_k = 100.0 * (close - lowest) / rng
    k_line = raw_k.rolling(smooth_k).mean()
    d_line = k_line.rolling(d).mean()
    s = f"{k}_{d}_{smooth_k}"
    return pd.DataFrame({
        f"STOCHk_{s}": k_line,
        f"STOCHd_{s}": d_line,
    }, index=close.index)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return _rma(tr, length)


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               length: int = 7, multiplier: float = 3.0) -> pd.DataFrame | None:
    high = pd.Series(high, dtype="float64").reset_index(drop=True)
    low = pd.Series(low, dtype="float64").reset_index(drop=True)
    close = pd.Series(close, dtype="float64").reset_index(drop=True)
    m = len(close)
    if m < length:
        return None

    atr = _atr(high, low, close, length)
    hl2 = (high + low) / 2.0
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    up = upper.to_numpy(copy=True)
    lo = lower.to_numpy(copy=True)
    cl = close.to_numpy()

    direction = np.ones(m, dtype="float64")
    trend = np.full(m, np.nan)
    long_band = np.full(m, np.nan)
    short_band = np.full(m, np.nan)

    for i in range(1, m):
        if cl[i] > up[i - 1]:
            direction[i] = 1
        elif cl[i] < lo[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
            if direction[i] > 0 and lo[i] < lo[i - 1]:
                lo[i] = lo[i - 1]
            if direction[i] < 0 and up[i] > up[i - 1]:
                up[i] = up[i - 1]
        if direction[i] > 0:
            trend[i] = long_band[i] = lo[i]
        else:
            trend[i] = short_band[i] = up[i]

    s = f"{length}_{multiplier}"
    out = pd.DataFrame({
        f"SUPERT_{s}": trend,
        f"SUPERTd_{s}": direction,
        f"SUPERTl_{s}": long_band,
        f"SUPERTs_{s}": short_band,
    })
    # NaN out the warm-up region where ATR is undefined
    out.loc[atr.isna().to_numpy(), :] = np.nan
    return out


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.DataFrame | None:
    high = pd.Series(high, dtype="float64")
    low = pd.Series(low, dtype="float64")
    close = pd.Series(close, dtype="float64")
    if len(close) < 2 * length:
        return None

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    atr = _atr(high, low, close, length)
    plus_di = 100.0 * _rma(plus_dm, length) / atr
    minus_di = 100.0 * _rma(minus_dm, length) / atr
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx_line = _rma(dx, length)

    return pd.DataFrame({
        f"ADX_{length}": adx_line,
        f"DMP_{length}": plus_di,
        f"DMN_{length}": minus_di,
    }, index=close.index)
