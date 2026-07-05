"""
All data-loading functions for the dashboard.
Every function is decorated with @st.cache_data so reads are not repeated on
every Streamlit re-run — only when the TTL expires or the file changes.

Path resolution: PROJECT_ROOT is two levels up from this file
  (dashboard/data_loader.py → dashboard/ → TradingAgent/)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TRADE_LOG_DIR   = PROJECT_ROOT / "data" / "trade_logs"
CHECKPOINT_DIR  = PROJECT_ROOT / "checkpoints"

PAPER_TRADES_FILE        = TRADE_LOG_DIR / "paper_trades.csv"
LIVE_TRADES_FILE         = TRADE_LOG_DIR / "live_paper_trades.csv"
WEIGHTS_FILE             = CHECKPOINT_DIR / "strategy_weights.json"
LIFETIME_WR_FILE         = CHECKPOINT_DIR / "strategy_lifetime_winrates.json"
STRATEGY_PERF_FILE       = CHECKPOINT_DIR / "strategy_performance.json"
LIVE_OPEN_TRADE_FILE     = CHECKPOINT_DIR / "live_open_trade.json"
STOCKS_DIR               = PROJECT_ROOT / "data" / "stocks"


# ── paper_trades.csv ─────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_paper_trades() -> pd.DataFrame:
    if not PAPER_TRADES_FILE.exists():
        return pd.DataFrame()
    df = pd.read_csv(PAPER_TRADES_FILE, parse_dates=["date"])
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.to_period("M").astype(str)
    return df


# ── historical parquet files (one per year) ──────────────────────────────────

@st.cache_data(ttl=300)
def load_historical_parquets(years: list[int] | None = None) -> pd.DataFrame:
    """
    Load trade parquets for the requested years and return a combined frame.
    Parquets have fewer columns than paper_trades.csv — do NOT concat with it.
    Only used for year-by-year P&L history on the P&L page.
    """
    frames = []
    for year_dir in sorted(TRADE_LOG_DIR.iterdir()):
        if not year_dir.is_dir():
            continue
        try:
            year = int(year_dir.name)
        except ValueError:
            continue
        if years and year not in years:
            continue
        pq = year_dir / "trades.parquet"
        if pq.exists():
            df = pd.read_parquet(pq)
            df["year"] = year
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ── checkpoint JSON files ─────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_strategy_weights() -> dict:
    if not WEIGHTS_FILE.exists():
        return {}
    return json.loads(WEIGHTS_FILE.read_text())


@st.cache_data(ttl=30)
def load_lifetime_winrates() -> dict:
    if not LIFETIME_WR_FILE.exists():
        return {}
    raw = json.loads(LIFETIME_WR_FILE.read_text())
    # Strip comment keys
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@st.cache_data(ttl=30)
def load_strategy_performance() -> dict[str, list]:
    """Returns {strategy_name: [0/0.5/1.0, ...]} rolling signal arrays."""
    if not STRATEGY_PERF_FILE.exists():
        return {}
    return json.loads(STRATEGY_PERF_FILE.read_text())


@st.cache_data(ttl=30)
def load_live_open_trade() -> dict | None:
    if not LIVE_OPEN_TRADE_FILE.exists():
        return None
    try:
        return json.loads(LIVE_OPEN_TRADE_FILE.read_text())
    except Exception:
        return None


@st.cache_data(ttl=30)
def load_live_trades() -> pd.DataFrame:
    if not LIVE_TRADES_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(LIVE_TRADES_FILE, parse_dates=["date"])
    except Exception:
        return pd.DataFrame()


# ── derived helpers ───────────────────────────────────────────────────────────

def apply_sidebar_filters(df: pd.DataFrame, symbol: str, years: list[int], phase: str) -> pd.DataFrame:
    """Filter paper_trades DataFrame by sidebar selections."""
    if df.empty:
        return df

    from config.settings import LEARNING_END_YEAR, TESTING_START_YEAR

    if phase == "Testing only":
        df = df[df["year"] >= TESTING_START_YEAR]
    elif phase == "Learning only":
        df = df[df["year"] <= LEARNING_END_YEAR]

    if years:
        df = df[df["year"].isin(years)]

    if symbol != "ALL":
        df = df[df["symbol"] == symbol]

    return df


def compute_equity_curve(df: pd.DataFrame, capital: float) -> pd.DataFrame:
    """Return df with cumulative equity and drawdown columns."""
    if df.empty:
        return df
    s = df.sort_values("date").copy()
    s["equity"] = capital + s["pnl_rs"].cumsum()
    s["drawdown"] = s["equity"] - s["equity"].cummax()
    return s


def explode_strategies(df: pd.DataFrame) -> pd.DataFrame:
    """Explode strategies_fired column into one strategy per row."""
    if df.empty or "strategies_fired" not in df.columns:
        return pd.DataFrame(columns=["strategy"])
    exploded = (
        df["strategies_fired"]
        .dropna()
        .str.split(",")
        .explode()
        .str.strip()
        .reset_index(drop=True)
    )
    return exploded.rename("strategy").to_frame()


# ── Candle data (Page 6) ──────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_candles(symbol: str, year: int) -> pd.DataFrame:
    """Load 5-min OHLCV parquet for a single symbol+year."""
    path = STOCKS_DIR / str(year) / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("Asia/Kolkata")
    return df


def available_symbols_for_year(year: int) -> list[str]:
    """Return sorted list of symbols that have a parquet file for the given year."""
    year_dir = STOCKS_DIR / str(year)
    if not year_dir.exists():
        return []
    return sorted(f.stem for f in year_dir.glob("*.parquet"))


def available_years() -> list[int]:
    """Return sorted list of years that have stock parquet folders."""
    if not STOCKS_DIR.exists():
        return []
    return sorted(
        int(d.name) for d in STOCKS_DIR.iterdir()
        if d.is_dir() and d.name.isdigit()
    )
