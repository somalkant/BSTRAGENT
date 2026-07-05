"""
Pre-Market Watchlist Filter — Stage 1 stock selector.

Runs before market opens (9:00-9:14 AM) on historical data only.
Reduces the intraday scan from 500 stocks to 20-25 high-probability
candidates, eliminating the scan-delay bottleneck entirely.

Two-stage scanning architecture:
  Stage 1 (this module): history_5min only → 20-25 watchlist stocks
  Stage 2 (engine.py):   today_5min only   → intraday strategy signals

Filters applied:
  Hard:  Liquidity ≥ 2 Crore avg daily turnover
  Hard:  Minimum 22 days of history
  Score: Daily trend bias   (weight 2.0) — MA20, MA50, RSI-14
  Score: Double bottom      (weight 1.5) — W-pattern in last 60 days
  Score: Falling wedge      (weight 1.5) — converging channel last 30 days
  Score: Ascending triangle (weight 1.5) — flat resistance + rising lows
  Score: Bull flag          (weight 1.5) — strong pole + consolidation
  Score: PDH proximity      (weight 0.5) — closed within 0.5% of day's high
"""

from __future__ import annotations
import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings import LIQUIDITY_MIN_TURNOVER, CHECKPOINT_DIR
from strategies.chart_patterns.double_bottom import DoubleBottom
from strategies.chart_patterns.falling_wedge import FallingWedge
from strategies.chart_patterns.ascending_triangle import AscendingTriangle
from strategies.chart_patterns.bull_flag import BullFlag

log = logging.getLogger(__name__)

# Reuse strategy instances for their _detect() logic — no signals fired here
_DBL = DoubleBottom()
_FW  = FallingWedge()
_AT  = AscendingTriangle()
_BF  = BullFlag()

# Liquidity threshold in Crores (settings stores it in absolute rupees)
_LIQUIDITY_CR = LIQUIDITY_MIN_TURNOVER / 1e7


class PreMarketFilter:
    """
    Scores all stocks using only historical data (no today_5min needed).
    Returns the top WATCHLIST_SIZE stocks for the intraday engine to scan.
    """
    WATCHLIST_SIZE = 200    # stocks passed to Stage 2 intraday scanner (100 bull + 100 bear)
    MIN_DAYS       = 22     # minimum trading days of history to participate

    # Score weights
    W_TREND   = 2.0         # daily trend alignment — strongest context signal
    W_PATTERN = 1.5         # any confirmed chart pattern
    W_PDH     = 0.5         # closed within PDH_BAND of yesterday's high
    PDH_BAND  = 0.005       # 0.5% proximity to previous day high

    def build(self, trade_date: date, all_data: dict) -> list[dict]:
        """
        Run pre-market filter on every stock in all_data.

        Args:
            trade_date: The trading day being analysed (data up to this date used)
            all_data:   Dict[symbol, DataFrame] — full historical 5-min data

        Returns:
            List of dicts, sorted by pre_score descending, max WATCHLIST_SIZE entries.
            Each dict: {symbol, pre_score, signals, turnover_cr}
        """
        results = []

        for symbol, df in all_data.items():
            history = df[df["datetime"].dt.date < trade_date]
            if history.empty:
                continue

            entry = self._score_stock(symbol, history)
            if entry is not None:
                results.append(entry)

        # Split: top half bullish (highest score) + top half bearish (most negative score)
        # This ensures short candidates reach the intraday engine alongside long candidates.
        half     = self.WATCHLIST_SIZE // 2
        bullish  = sorted([r for r in results if r["pre_score"] > 0],
                          key=lambda x: -x["pre_score"])
        bearish  = sorted([r for r in results if r["pre_score"] < 0],
                          key=lambda x: x["pre_score"])   # most negative first
        watchlist = bullish[:half] + bearish[:half]

        log.info(
            f"Pre-market {trade_date}: {len(all_data)} stocks → "
            f"{len(results)} liquid+signal ({len(bullish)} bull / {len(bearish)} bear) "
            f"→ {len(watchlist)} watchlist"
        )
        return watchlist

    def _score_stock(self, symbol: str, history_5min: pd.DataFrame) -> dict | None:
        # ── Hard filter 1: liquidity ──────────────────────────────────────────
        turnover_cr = _avg_turnover_cr(history_5min)
        if turnover_cr < _LIQUIDITY_CR:
            return None

        # ── Hard filter 2: minimum history ───────────────────────────────────
        daily = _to_daily(history_5min, 65)   # 65 covers DBL-BTM 60-day lookback
        if len(daily) < self.MIN_DAYS:
            return None

        score   = 0.0
        signals = []

        # ── Daily trend bias (MA20 / MA50 / RSI-14) ───────────────────────────
        trend_dir, trend_reason = _check_trend(daily)
        if trend_dir == 1:
            score += self.W_TREND
            signals.append(("DAILY-BIAS", trend_reason))
        elif trend_dir == -1:
            # Bearish bias suppresses the score — we only take long trades
            score -= self.W_TREND
            signals.append(("DAILY-BIAS-BEAR", trend_reason))

        # ── Chart patterns (all four are bullish setups) ──────────────────────
        try:
            pat = _DBL._detect(daily.tail(60))
            if pat:
                score += self.W_PATTERN
                signals.append(("DBL-BTM", f"neckline={pat['neckline']:.2f}"))
        except Exception:
            pass

        try:
            pat = _FW._detect(daily.tail(30))
            if pat:
                score += self.W_PATTERN
                signals.append(("FALL-WEDGE", f"upper={pat['upper']:.2f}"))
        except Exception:
            pass

        try:
            pat = _AT._detect(daily.tail(30))
            if pat:
                score += self.W_PATTERN
                signals.append(("ASC-TRI", f"resistance={pat['resistance']:.2f}"))
        except Exception:
            pass

        try:
            lookback = _BF.POLE_DAYS + _BF.FLAG_DAYS + 2
            pat = _BF._detect(daily.tail(lookback))
            if pat:
                score += self.W_PATTERN
                signals.append(("BULL-FLAG", f"flag_top={pat['flag_top']:.2f}"))
        except Exception:
            pass

        # ── PDH proximity bonus ───────────────────────────────────────────────
        if len(daily) >= 2:
            pdh    = float(daily["high"].iloc[-1])
            last_c = float(daily["close"].iloc[-1])
            if pdh > 0 and abs(last_c - pdh) / pdh <= self.PDH_BAND:
                score += self.W_PDH
                signals.append(("PDH-CLOSE", f"closed near PDH {pdh:.2f}"))

        # Pass stocks with any meaningful signal (bullish or bearish)
        if abs(score) < 0.1:
            return None

        return {
            "symbol":      symbol,
            "pre_score":   round(score, 2),
            "signals":     signals,
            "turnover_cr": round(turnover_cr, 1),
        }


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS (module-level so they can be tested independently)
# ─────────────────────────────────────────────────────────────────────────────

def _to_daily(history_5min: pd.DataFrame, n: int) -> pd.DataFrame:
    """Aggregate 5-min OHLCV to daily OHLCV, return last n days."""
    return (history_5min
            .groupby(history_5min["datetime"].dt.date)
            .agg(open=("open", "first"), high=("high", "max"),
                 low=("low", "min"),    close=("close", "last"),
                 volume=("volume", "sum"))
            .tail(n))


def _avg_turnover_cr(history_5min: pd.DataFrame, days: int = 20) -> float:
    """Average daily close×volume turnover in Crores over last `days` sessions."""
    recent = history_5min.tail(days * 75)   # 75 candles per day
    if recent.empty:
        return 0.0
    daily = (recent.groupby(recent["datetime"].dt.date)
             .apply(lambda x: (x["close"] * x["volume"]).sum() / 1e7))
    return float(daily.median()) if not daily.empty else 0.0


def _check_trend(daily: pd.DataFrame) -> tuple[int, str]:
    """
    Daily trend bias check — mirrors DailyTrendBias strategy logic.
    Returns (+1, reason) bullish, (-1, reason) bearish, (0, '') neutral.
    """
    closes = daily["close"].values.astype(float)
    n      = len(closes)
    if n < 22:
        return 0, ""

    current   = closes[-1]
    ma20      = float(np.mean(closes[-20:]))
    ma20_prev = float(np.mean(closes[-21:-1])) if n >= 21 else ma20
    ma50      = float(np.mean(closes[-50:])) if n >= 50 else float(np.mean(closes))

    # RSI-14 (simple non-smoothed)
    deltas   = np.diff(closes)
    gains    = np.where(deltas > 0, deltas, 0.0)[-14:]
    losses   = np.where(deltas < 0, -deltas, 0.0)[-14:]
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    rsi      = (100.0 - 100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0

    bullish = [
        current > ma20,      # price above 20-day MA
        ma20 > ma20_prev,    # 20-day MA rising
        rsi > 50,            # daily momentum positive
        current > ma50,      # price above 50-day MA
    ]
    bull = sum(bullish)
    bear = sum(not c for c in bullish)

    if bull >= 3:
        return +1, f"uptrend {bull}/4 MA20={ma20:.0f} RSI={rsi:.0f}"
    if bear >= 3:
        return -1, f"downtrend {bear}/4 MA20={ma20:.0f} RSI={rsi:.0f}"
    return 0, ""


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────

def save_watchlist(watchlist: list[dict], trade_date: date) -> Path:
    """Save watchlist to checkpoints/watchlist_{date}.json."""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKPOINT_DIR / f"watchlist_{trade_date}.json"
    with open(path, "w") as f:
        json.dump({"trade_date": str(trade_date), "watchlist": watchlist}, f, indent=2)
    return path


def load_watchlist(trade_date: date) -> list[str] | None:
    """
    Load symbols from a saved watchlist file.
    Returns None if no watchlist was saved for that date (triggers full scan fallback).
    """
    path = CHECKPOINT_DIR / f"watchlist_{trade_date}.json"
    if not path.exists():
        return None
    with open(path) as f:
        data = json.load(f)
    return [entry["symbol"] for entry in data.get("watchlist", [])]
