"""
Logs live paper trades to data/trade_logs/live_paper_trades.csv.

Separate from the backtesting paper_trades.csv — this is for live Phase 2.5 data.
Same schema as backtesting paper_trades.csv plus live-only columns (entry_time, strategy_entry).

Crash safety: open trade state is also saved to checkpoints/live_open_trade.json
so the agent can resume monitoring after a restart.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from config.settings import TRADE_LOG_DIR
from backtester.cost_model import net_pnl

log = logging.getLogger(__name__)

LIVE_TRADES_FILE  = TRADE_LOG_DIR / "live_paper_trades.csv"
OPEN_TRADE_CACHE  = Path(__file__).parent.parent / "checkpoints" / "live_open_trade.json"

_COLUMNS = [
    "date", "symbol", "signal_time", "entry_time", "strategy_entry", "entry_price",
    "quantity", "position_rs", "stop_loss", "target", "rr", "strategies_fired",
    "agreeing_count", "composite_score", "driver_strategy", "reason", "exit_time",
    "exit_price", "exit_reason", "result", "pnl_rs", "pnl_pct", "predicted_win_pct",
    "conviction_tier",
]


def save_open_trade(trade_date: date,
                    long_rec:  dict | None,
                    short_rec: dict | None) -> None:
    """
    Persist open trade state to disk (up to one LONG + one SHORT).
    Deletes the checkpoint when both slots are None (all positions closed).
    """
    if long_rec is None and short_rec is None:
        clear_open_trade()
        return
    payload = {"date": str(trade_date), "long": long_rec, "short": short_rec}
    OPEN_TRADE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    OPEN_TRADE_CACHE.write_text(json.dumps(payload, default=str))
    symbols = [r["symbol"] for r in (long_rec, short_rec) if r]
    log.debug(f"Open trade checkpoint saved: {', '.join(symbols)}")


def load_open_trade() -> tuple[date | None, dict | None, dict | None]:
    """
    Load persisted open trades from a previous run of the agent today.
    Returns (trade_date, long_rec, short_rec); any slot is None if not open.
    """
    if not OPEN_TRADE_CACHE.exists():
        return None, None, None
    try:
        data = json.loads(OPEN_TRADE_CACHE.read_text())
        saved_date = date.fromisoformat(data["date"])
        if saved_date != date.today():
            return None, None, None   # stale from a previous day
        long_rec  = data.get("long")
        short_rec = data.get("short")
        # backward-compat: old format stored a single "rec" key
        if long_rec is None and short_rec is None and "rec" in data:
            dirn = data["rec"].get("direction", "LONG")
            if dirn == "SHORT":
                short_rec = data["rec"]
            else:
                long_rec = data["rec"]
        symbols = [r["symbol"] for r in (long_rec, short_rec) if r]
        log.info(f"Resumed open trade(s) from checkpoint: {', '.join(symbols)}")
        return saved_date, long_rec, short_rec
    except Exception as e:
        log.warning(f"Could not read open trade checkpoint: {e}")
        return None, None, None


def clear_open_trade() -> None:
    """Remove the open trade checkpoint after the trade is closed."""
    if OPEN_TRADE_CACHE.exists():
        OPEN_TRADE_CACHE.unlink()


def log_closed_trade(
    trade_date: date,
    rec: dict,
    exit_price: float,
    exit_reason: str,
    exit_time: str,
) -> None:
    """
    Append a completed trade to live_paper_trades.csv.
    Called when exit_reason is known (TARGET_HIT / STOP_HIT / TIME_EXIT).
    """
    sig      = rec["signal"]
    entry    = float(sig["entry"])
    pos_rs   = float(rec["position_rs"])
    shares   = int(rec["shares"])

    # Net P&L after brokerage, STT, exchange, GST, stamp, slippage — same model as backtester
    direction_int = 1 if rec.get("direction", "LONG") == "LONG" else -1
    raw_pnl  = net_pnl(entry, exit_price, shares, direction=direction_int)
    pnl_pct  = round(raw_pnl / (entry * shares) * 100, 2)

    result = _result_label(exit_reason, raw_pnl)

    row = {
        "date":             str(trade_date),
        "symbol":           rec["symbol"],
        "signal_time":      sig["signal_time"],
        "entry_time":       rec.get("entry_time", ""),
        "strategy_entry":   sig.get("strategy_entry", entry),
        "entry_price":      entry,
        "quantity":         shares,
        "position_rs":      round(pos_rs, 2),
        "stop_loss":        sig["stop"],
        "target":           sig["target"],
        "rr":               sig["rr"],
        "strategies_fired": rec.get("strategies_fired", ""),
        "agreeing_count":   rec["agreeing"],
        "composite_score":  round(rec["score"], 3),
        "driver_strategy":  sig["strategy"],
        "reason":           sig.get("reason", ""),
        "exit_time":        exit_time,
        "exit_price":       round(exit_price, 2),
        "exit_reason":      exit_reason,
        "result":           result,
        "pnl_rs":           round(raw_pnl, 2),
        "pnl_pct":          pnl_pct,
        "predicted_win_pct": rec.get("predicted_win_pct", 50.0),
        "conviction_tier":  rec.get("conviction_tier", "STANDARD"),
    }

    new_df = pd.DataFrame([row], columns=_COLUMNS)
    LIVE_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = not LIVE_TRADES_FILE.exists()
    new_df.to_csv(LIVE_TRADES_FILE, mode="a", header=header, index=False)

    pnl_sign = "+" if raw_pnl >= 0 else ""
    log.info(
        f"TRADE CLOSED: {rec['symbol']} | "
        f"entered={rec.get('entry_time', sig['signal_time'])} exited={exit_time} | "
        f"{exit_reason} @ {exit_price:.2f} | "
        f"P&L Rs {pnl_sign}{raw_pnl:,.0f} ({pnl_pct:+.2f}%) | {result}"
    )
    # Checkpoint is managed by agent.py — it updates save_open_trade() after exit.


def _result_label(exit_reason: str, pnl: float) -> str:
    if exit_reason == "TARGET_HIT":
        return "EXACT_WIN"
    return "WIN" if pnl > 0 else "LOSS"
