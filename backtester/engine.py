"""
Sequential Day-by-Day Backtester Engine — Phase 2B (Long + Short)

Processes one year of data one trading day at a time, simulating what a
live system would do with only past data available.

Phase 2B changes:
  - Weights are {"long": x, "short": y} per strategy
  - Finds best LONG candidate AND best SHORT candidate each day
  - Applies direction bias (VIX / Nifty regime) to pick the better one
  - Updates perf_long from +1 trade outcomes, perf_short from -1 trade outcomes
  - paper_trades.csv gains a "direction" column (LONG / SHORT)

Usage:
    from backtester.engine import run_year
    results = run_year(year=2016)
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
from tqdm import tqdm

from config.settings import (
    STOCKS_DIR, INDEX_DIR, PROGRESS_FILE,
    WEIGHTS_FILE, MEMORY_DIR, NOTEBOOKS_DIR, CHECKPOINT_DIR,
    WEIGHT_UPDATE_EVERY, MAX_RECOMMENDATIONS,
    LEARNING_END_YEAR, TRADE_LOG_DIR, DAILY_LOSS_LIMIT,
    PAPER_TRADES_FILE, TESTING_MAX_RECOMMENDATIONS, AGREEMENT_MIN_LIFETIME_WR,
    CONVICTION_HIGH_WR, CONVICTION_MED_WR, CONVICTION_HIGH_MULT, CONVICTION_MED_MULT,
    SHORT_ENABLED,
)
from strategies import ALL_STRATEGIES, STRATEGY_NAMES
from backtester.composite_scorer import (
    long_composite_score, short_composite_score,
    count_agreeing, count_agreeing_filtered,
)
from backtester.quality_filter import passes_all_filters
from backtester.position_sizer import position_size
from backtester.cost_model import net_pnl
from weights.adaptive import update_weights
from weights.regime import get_regime_modifiers, get_direction_bias
from watchlist.pre_filter import PreMarketFilter

log = logging.getLogger(__name__)

_PRE_FILTER = PreMarketFilter()


def _load_lifetime_winrates(path: Path | None = None) -> dict:
    if path is None:
        path = CHECKPOINT_DIR / "strategy_lifetime_winrates.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


_LIFETIME_WR: dict = _load_lifetime_winrates()


def reload_lifetime_winrates(path: Path | str) -> None:
    """Reload _LIFETIME_WR from a different file (used by short WR optimization)."""
    global _LIFETIME_WR
    _LIFETIME_WR = _load_lifetime_winrates(Path(path))


def _get_wr(strategy: str, direction: int) -> float:
    """Direction-specific lifetime win rate (0–100). Falls back to 50.0 if unknown."""
    entry = _LIFETIME_WR.get(strategy)
    if entry is None:
        return 50.0
    if isinstance(entry, dict):
        key = "long" if direction == +1 else "short"
        return float(entry.get(key, 50.0))
    return float(entry)  # backward compat: plain float value


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def run_year(year: int, use_pre_filter: bool = True,
             wf_weights_file: Path | None = None,
             wr_file: Path | None = None) -> dict:
    """
    Process all trading days in a year sequentially.
    Resumes from last checkpoint if interrupted.

    wf_weights_file: if provided, load frozen Walk-Forward weights from this
                     file instead of the live strategy_weights.json (testing mode).
                     Uses its own wf{N}_progress.json so it never corrupts
                     the analysis progress checkpoint.
    use_pre_filter:  if True (default), run Stage 1 pre-market filter each day.
    wr_file:         if provided, override lifetime win rates from this JSON file
                     instead of the default strategy_lifetime_winrates.json.
    """
    if wr_file is not None:
        reload_lifetime_winrates(wr_file)
    # WF testing gets its own isolated checkpoint files (e.g. wf2_progress.json)
    if wf_weights_file is not None:
        wf_prefix   = wf_weights_file.stem.replace("_weights", "")   # "wf2"
        prog_file   = wf_weights_file.parent / f"{wf_prefix}_progress.json"
        perf_long_f  = f"{wf_prefix}_perf_long.json"
        perf_short_f = f"{wf_prefix}_perf_short.json"
    else:
        prog_file   = PROGRESS_FILE
        perf_long_f  = "strategy_performance_long.json"
        perf_short_f = "strategy_performance_short.json"

    progress   = _load_progress(prog_file)
    weights    = _load_weights(wf_weights_file)
    perf_long  = _load_performance(perf_long_f)
    perf_short = _load_performance(perf_short_f)

    phase = "LEARNING" if year <= LEARNING_END_YEAR else "TESTING"
    freeze_weights = (phase == "TESTING") or (wf_weights_file is not None)

    log.info(f"Starting {year} [{phase}] — weights {'FROZEN' if freeze_weights else 'adaptive'} "
             f"— SHORT {'ON' if SHORT_ENABLED else 'OFF'} "
             f"— pre-filter {'ON' if use_pre_filter else 'OFF'}")

    log.info("Loading stock data into memory...")
    all_data, nifty_data = _preload_data(year)
    trading_days = _get_trading_days(all_data, year)

    analysis  = progress.get("analysis", {})
    year_str  = str(year)
    last_done = analysis.get(year_str, {}).get("last_processed_date")
    if last_done:
        start_from   = date.fromisoformat(last_done) + timedelta(days=1)
        trading_days = [d for d in trading_days if d >= start_from]
        log.info(f"Resuming from {start_from} — {len(trading_days)} days remaining")

    day_results         = []
    days_since_update   = analysis.get(year_str, {}).get("days_since_weight_update", 0)

    for trade_date in tqdm(trading_days, desc=f"Analysing {year}", unit="day"):
        day_result = _process_day(
            trade_date, all_data, nifty_data,
            weights, perf_long, perf_short, freeze_weights, use_pre_filter
        )
        day_results.append(day_result)
        days_since_update += 1

        for rec in day_result.get("recommendations", []):
            sig     = rec["signal"]
            out     = rec["outcome"]
            verdict = out["exit_reason"]   # EXACT_WIN / WIN / LOSS / TIME_EXIT
            pnl     = rec["pnl_rs"]
            sign    = "+" if pnl >= 0 else ""
            log.info(
                f"TRADE {trade_date}  {rec['symbol']:<12} {rec['direction']:<5} "
                f"driver={sig['strategy']:<16} "
                f"entry={sig['entry']:.2f}@{sig['signal_time']}  "
                f"exit={out['exit_price']:.2f}@{out.get('exit_time','?')}  "
                f"{verdict:<10}  P&L Rs {sign}{pnl:,.0f}"
            )

        if not freeze_weights and days_since_update >= WEIGHT_UPDATE_EVERY:
            weights = update_weights(weights, perf_long, perf_short,
                                     vix=day_result.get("vix", 15))
            days_since_update = 0
            _save_weights(weights)
            log.info(f"Weights updated after {trade_date}")

        _save_day_checkpoint(progress, year_str, trade_date, days_since_update,
                             perf_long, perf_short,
                             prog_file, perf_long_f, perf_short_f)
        _append_trade_log(year, day_result)
        if freeze_weights:
            _append_paper_trade_log(day_result)

    summary = _year_summary(year, day_results, weights, perf_long, perf_short)
    _write_year_summary(year, summary)
    log.info(f"Year {year} complete: {summary['total_trades']} trades | "
             f"effective win rate {summary['effective_win_rate']}% | "
             f"P&L Rs {summary['total_pnl']:,.0f}")
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# CORE DAY PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

def _process_day(trade_date, all_data, nifty_data, weights,
                 perf_long, perf_short, freeze_weights,
                 use_pre_filter: bool = True) -> dict:
    today_str   = str(trade_date)
    nifty_today = _get_today(nifty_data, trade_date) if nifty_data is not None else pd.DataFrame()

    vix          = _get_vix(nifty_today)
    nifty_pct    = _get_nifty_pct_change(nifty_today, nifty_data, trade_date)
    regime_mods  = get_regime_modifiers(weights, vix=vix)
    long_bias, short_bias = get_direction_bias(vix, nifty_pct)

    if use_pre_filter:
        watchlist     = _PRE_FILTER.build(trade_date, all_data)
        watchlist_set = {e["symbol"] for e in watchlist}
        scan_data     = {s: df for s, df in all_data.items() if s in watchlist_set}
        if not scan_data:
            scan_data = all_data
            log.warning(f"{trade_date}: pre-filter empty — falling back to full scan")
    else:
        scan_data = all_data

    # ── Per-stock signal generation ──────────────────────────────────────────
    stock_signals       = {}   # {symbol: {strategy_name: Signal}}
    stock_long_scores   = {}   # {symbol: (raw_score, adj_score)}
    stock_short_scores  = {}   # {symbol: (raw_score, adj_score)}

    for symbol, df in scan_data.items():
        today_5min   = _get_today(df, trade_date)
        history_5min = df[df["datetime"].dt.date < trade_date]

        if today_5min.empty or len(today_5min) < 5:
            continue

        prev_day = _get_prev_day_ohlc(history_5min, trade_date)

        signals = {}
        for strategy in ALL_STRATEGIES:
            try:
                sig = strategy.generate_signal(
                    today_5min   = today_5min,
                    history_5min = history_5min,
                    prev_day     = prev_day,
                    nifty_today  = nifty_today,
                    trade_date   = trade_date,
                )
                signals[strategy.name] = sig
            except Exception:
                from strategies.base import Signal
                signals[strategy.name] = Signal(strategy=strategy.name, direction=0)

        stock_signals[symbol] = signals

        ls_raw = long_composite_score(signals, weights, regime_mods)
        ss_raw = short_composite_score(signals, weights, regime_mods)
        stock_long_scores[symbol]  = (ls_raw, ls_raw * long_bias)
        stock_short_scores[symbol] = (ss_raw, ss_raw * short_bias)

    # ── Find best LONG candidate ─────────────────────────────────────────────
    daily_turnover = _estimate_turnover(all_data, trade_date)
    best_long_rec  = _find_best_candidate(
        direction=+1,
        stock_scores=stock_long_scores,
        stock_signals=stock_signals,
        all_data=all_data,
        trade_date=trade_date,
        daily_turnover=daily_turnover,
        weights=weights,
        freeze_weights=freeze_weights,
        nifty_pct=nifty_pct,
    )

    # ── Find best SHORT candidate ────────────────────────────────────────────
    best_short_rec = None
    if SHORT_ENABLED:
        best_short_rec = _find_best_candidate(
            direction=-1,
            stock_scores=stock_short_scores,
            stock_signals=stock_signals,
            all_data=all_data,
            trade_date=trade_date,
            daily_turnover=daily_turnover,
            weights=weights,
            freeze_weights=freeze_weights,
            nifty_pct=nifty_pct,
        )

    # ── Direction picker ──────────────────────────────────────────────────────
    # Phase 2B dual-direction mode: take 1 LONG + 1 SHORT independently per day.
    # Both sides calibrate independently — critical for live 1L+1S operation.
    # Falls back to single-direction pick when SHORT_ENABLED is False (legacy).
    if SHORT_ENABLED:
        recommendations = [r for r in (best_long_rec, best_short_rec) if r]
        _log_direction_race(trade_date, best_long_rec, best_short_rec, None)
    else:
        day_rec = _pick_direction(best_long_rec, best_short_rec)
        recommendations = [day_rec] if day_rec else []
        _log_direction_race(trade_date, best_long_rec, best_short_rec, day_rec)

    # ── Update performance tracking ──────────────────────────────────────────
    for day_rec in recommendations:
        sig_dict  = day_rec["signal"]
        direction = sig_dict["direction"]
        signals   = stock_signals.get(day_rec["symbol"], {})
        outcome   = day_rec["outcome"]
        pnl       = day_rec["pnl_rs"]
        if direction == +1:
            _update_perf(perf_long, signals, direction, outcome["exit_reason"], pnl)
        else:
            _update_perf(perf_short, signals, direction, outcome["exit_reason"], pnl)

    return {
        "date":            today_str,
        "recommendations": recommendations,
        "total_pnl":       sum(r["pnl_rs"] for r in recommendations),
        "vix":             vix,
        "nifty_pct":       nifty_pct,
    }


def _find_best_candidate(
    direction: int,
    stock_scores: dict,
    stock_signals: dict,
    all_data: dict,
    trade_date,
    daily_turnover: dict,
    weights: dict,
    freeze_weights: bool,
    nifty_pct: float,
) -> dict | None:
    """
    Find the best passing candidate in the given direction (+1 or -1).
    stock_scores = {symbol: (raw_score, adj_score)}
    Returns a recommendation dict or None.
    """
    sorted_stocks = sorted(stock_scores.items(), key=lambda x: x[1][1], reverse=True)

    for symbol, (raw_score, adj_score) in sorted_stocks[:20]:
        if raw_score <= 0:
            break

        signals  = stock_signals.get(symbol)
        if not signals:
            continue
        today_5m = _get_today(all_data[symbol], trade_date)

        best_sig = _best_signal(signals, direction=direction)
        if best_sig is None:
            continue

        if freeze_weights:
            agreeing = count_agreeing_filtered(signals, direction, _LIFETIME_WR,
                                               AGREEMENT_MIN_LIFETIME_WR)
        else:
            agreeing = count_agreeing(signals, direction=direction)

        turnover     = daily_turnover.get(symbol, 0)
        pred_win_pct = _predicted_win_pct(signals, weights, _LIFETIME_WR, direction=direction)

        # Compute short-specific filter inputs
        week52_low      = _get_week52_low(all_data[symbol], trade_date) if direction == -1 else 0.0
        recent_3day_mv  = _recent_move(all_data[symbol], trade_date, days=3) if direction == -1 else 0.0

        passes, reason = passes_all_filters(
            signal               = best_sig,
            today_5min           = today_5m,
            daily_turnover_crore = turnover,
            strategies_agreeing  = agreeing,
            composite_score      = raw_score,   # raw score for quality gating (not biased)
            predicted_win_pct    = pred_win_pct,
            week52_low           = week52_low,
            nifty_pct_change     = nifty_pct,
            recent_3day_move     = recent_3day_mv,
        )

        if not passes:
            continue

        conv_mult, conv_tier = _conviction_multiplier(best_sig.strategy, direction)
        rs_value, shares     = position_size(best_sig.entry, best_sig.stop, conv_mult)
        outcome              = _simulate_outcome(best_sig, today_5m)
        pnl                  = net_pnl(best_sig.entry, outcome["exit_price"], shares,
                                       direction=direction)

        return {
            "symbol":            symbol,
            "score":             adj_score,    # bias-adjusted score for direction comparison
            "raw_score":         raw_score,
            "signal":            best_sig.to_dict(),
            "agreeing":          agreeing,
            "position_rs":       rs_value,
            "shares":            shares,
            "outcome":           outcome,
            "pnl_rs":            round(pnl, 2),
            "strategies_fired":  _strategies_fired(signals, direction),
            "predicted_win_pct": pred_win_pct,
            "conviction_tier":   conv_tier,
            "conviction_mult":   conv_mult,
            "direction":         "LONG" if direction == +1 else "SHORT",
        }

    return None


def _pick_direction(long_rec: dict | None, short_rec: dict | None) -> dict | None:
    """Pick 1 trade per day: whichever direction has the higher adjusted score."""
    if long_rec and short_rec:
        return long_rec if long_rec["score"] >= short_rec["score"] else short_rec
    return long_rec or short_rec


def _log_direction_race(trade_date, long_rec, short_rec, picked) -> None:
    def _fmt(rec: dict | None) -> str:
        if rec is None:
            return "none"
        sig = rec["signal"]
        return (
            f"{rec['symbol']}/{sig['strategy']}"
            f" s={rec['score']:.2f} a={rec['agreeing']}"
            f" p={rec['predicted_win_pct']:.0f}%"
            f" [{rec['conviction_tier']}]"
        )

    long_str  = _fmt(long_rec)
    short_str = _fmt(short_rec)

    if picked is None and (long_rec or short_rec):
        decision = "BOTH TAKEN"
    elif picked is None:
        decision = "NO TRADE"
    elif long_rec and short_rec:
        loser = short_rec if picked["direction"] == "LONG" else long_rec
        decision = f"-> {picked['direction']} wins (score {picked['score']:.2f} > {loser['score']:.2f})"
    else:
        decision = f"-> {picked['direction']} (only candidate)"

    log.info(f"{trade_date}  LONG=[{long_str}]  SHORT=[{short_str}]  {decision}")


# ─────────────────────────────────────────────────────────────────────────────
# TRADE SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_outcome(signal, today_5min: pd.DataFrame) -> dict:
    from datetime import time as dtime

    entry  = signal.entry
    target = signal.target
    stop   = signal.stop
    dirn   = signal.direction

    sig_time = signal.signal_time or "09:15"
    try:
        h, m   = map(int, sig_time.split(":"))
        sig_dt = dtime(h, m)
    except Exception:
        sig_dt = dtime(9, 15)

    exit_dt     = dtime(15, 15)
    exit_price  = entry
    exit_reason = "TIME_EXIT"
    exit_time   = "15:15"

    for _, c in today_5min.iterrows():
        t = pd.Timestamp(c["datetime"]).time()
        if t <= sig_dt:
            continue
        if t >= exit_dt:
            exit_price  = float(c["open"])
            exit_reason = "TIME_EXIT"
            exit_time   = t.strftime("%H:%M")
            break

        if dirn == 1:
            if c["high"] >= target:
                exit_price, exit_reason, exit_time = target, "TARGET_HIT", t.strftime("%H:%M")
                break
            if c["low"] <= stop:
                exit_price, exit_reason, exit_time = stop, "STOP_HIT", t.strftime("%H:%M")
                break
            exit_price = float(c["close"])
            exit_time  = t.strftime("%H:%M")

        elif dirn == -1:
            if c["low"] <= target:
                exit_price, exit_reason, exit_time = target, "TARGET_HIT", t.strftime("%H:%M")
                break
            if c["high"] >= stop:
                exit_price, exit_reason, exit_time = stop, "STOP_HIT", t.strftime("%H:%M")
                break
            exit_price = float(c["close"])
            exit_time  = t.strftime("%H:%M")

    return {"exit_price": exit_price, "exit_reason": exit_reason, "exit_time": exit_time}


# ─────────────────────────────────────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _preload_data(year: int) -> tuple[dict, pd.DataFrame | None]:
    all_data: dict[str, pd.DataFrame] = {}

    for y in [year - 1, year]:
        yr_dir = STOCKS_DIR / str(y)
        if not yr_dir.exists():
            continue
        for f in yr_dir.glob("*.parquet"):
            try:
                df = pd.read_parquet(f)
                df["datetime"] = pd.to_datetime(df["datetime"])
                stem = f.stem
                if stem in all_data:
                    combined = pd.concat([all_data[stem], df])
                    combined = (combined.drop_duplicates("datetime")
                                        .sort_values("datetime")
                                        .reset_index(drop=True))
                    all_data[stem] = combined
                else:
                    all_data[stem] = df
            except Exception:
                pass

    nifty_dfs = []
    for y in [year - 1, year]:
        nf = INDEX_DIR / str(y) / "NIFTY50.parquet"
        if nf.exists():
            df = pd.read_parquet(nf)
            df["datetime"] = pd.to_datetime(df["datetime"])
            nifty_dfs.append(df)
    nifty = None
    if nifty_dfs:
        nifty = (pd.concat(nifty_dfs)
                   .drop_duplicates("datetime")
                   .sort_values("datetime")
                   .reset_index(drop=True))

    log.info(f"Loaded {len(all_data)} stocks for {year} (with prev-year warmup)")
    return all_data, nifty


def _get_trading_days(all_data: dict, year: int) -> list:
    if not all_data:
        return []
    all_days: set = set()
    for df in all_data.values():
        year_rows = df[df["datetime"].dt.year == year]
        all_days.update(year_rows["datetime"].dt.date.unique())
    return sorted(all_days)


def _get_today(df: pd.DataFrame, trade_date: date) -> pd.DataFrame:
    return df[df["datetime"].dt.date == trade_date].copy().reset_index(drop=True)


def _get_prev_day_ohlc(history: pd.DataFrame, trade_date: date) -> pd.Series | None:
    if history.empty:
        return None
    daily = (history.groupby(history["datetime"].dt.date)
             .agg(open=("open","first"), high=("high","max"),
                  low=("low","min"), close=("close","last"),
                  volume=("volume","sum")))
    if daily.empty:
        return None
    return daily.iloc[-1]


def _get_vix(nifty_today: pd.DataFrame) -> float:
    return 15.0   # Default — India VIX file would override in production


def _get_nifty_pct_change(nifty_today: pd.DataFrame,
                           nifty_all: pd.DataFrame | None,
                           trade_date: date) -> float:
    """
    Estimate today's Nifty opening gap vs previous day's close.
    Uses first candle open vs prior day close — available at 9:20 AM, no look-ahead.
    """
    if nifty_today is None or nifty_today.empty:
        return 0.0
    if nifty_all is None:
        return 0.0
    prev_day = nifty_all[nifty_all["datetime"].dt.date < trade_date]
    if prev_day.empty:
        return 0.0
    prev_close = float(prev_day.iloc[-1]["close"])
    today_open = float(nifty_today.iloc[0]["open"])
    if prev_close <= 0:
        return 0.0
    return round((today_open - prev_close) / prev_close * 100, 3)


def _estimate_turnover(all_data: dict, trade_date: date) -> dict:
    result = {}
    for symbol, df in all_data.items():
        recent = df[df["datetime"].dt.date < trade_date].tail(20 * 75)
        if recent.empty:
            result[symbol] = 0
            continue
        daily_turnover = (recent.groupby(recent["datetime"].dt.date)
                          .apply(lambda x: (x["close"] * x["volume"]).sum() / 1e7))
        result[symbol] = float(daily_turnover.median()) if not daily_turnover.empty else 0
    return result


def _get_week52_low(df: pd.DataFrame, trade_date: date) -> float:
    """52-week low from history up to trade_date (no look-ahead)."""
    one_year_ago = trade_date - timedelta(days=365)
    hist = df[(df["datetime"].dt.date >= one_year_ago) &
              (df["datetime"].dt.date < trade_date)]
    if hist.empty:
        return 0.0
    return float(hist["low"].min())


def _recent_move(df: pd.DataFrame, trade_date: date, days: int = 3) -> float:
    """Abs % price move of stock over last N trading days (news proxy for Filter 10)."""
    hist = df[df["datetime"].dt.date < trade_date]
    if hist.empty:
        return 0.0
    daily = (hist.groupby(hist["datetime"].dt.date)
             .agg(close=("close","last"))).tail(days)
    if len(daily) < 2:
        return 0.0
    start_p = float(daily.iloc[0]["close"])
    end_p   = float(daily.iloc[-1]["close"])
    if start_p <= 0:
        return 0.0
    return abs(end_p - start_p) / start_p


def _best_signal(signals: dict, direction: int):
    """
    Best driver signal for the given direction using lifetime_win_rate × RR.
    DRIVER_BLOCKED strategies may vote but cannot set the trade target.
    """
    candidates = [
        s for s in signals.values()
        if s.direction == direction and s.is_valid and s.strategy not in DRIVER_BLOCKED
    ]
    if not candidates:
        return None

    def _driver_score(s) -> float:
        wr = _get_wr(s.strategy, direction) / 100.0
        return wr * s.rr

    return max(candidates, key=_driver_score)


DRIVER_BLOCKED = {
    "VOL-SPIKE",
    "ORB-30",
    "ORB-15",
    "SR-BREAK",
    "REL-STR",
    "DESC-TRI",   # 3-year confirmed: 27 driver trades, 0 target hits, Rs -11,247 net
}
CONVICTION_BLOCKED = DRIVER_BLOCKED


def _conviction_multiplier(driver_strategy: str, direction: int = +1) -> tuple[float, str]:
    if driver_strategy in CONVICTION_BLOCKED:
        return 1.0, "STANDARD"
    wr = _get_wr(driver_strategy, direction)
    if wr >= CONVICTION_HIGH_WR:
        return CONVICTION_HIGH_MULT, "HIGH"
    elif wr >= CONVICTION_MED_WR:
        return CONVICTION_MED_MULT, "MEDIUM"
    return 1.0, "STANDARD"


def _update_perf(perf: dict, signals: dict, direction: int,
                 result: str, pnl: float = 0.0) -> None:
    """Update performance for strategies that fired in the given direction."""
    if result == "TARGET_HIT":
        score = 1.0
    elif result == "TIME_EXIT" and pnl > 0:
        score = 0.5
    else:
        score = 0.0

    for name, sig in signals.items():
        if sig.direction == direction:
            if name not in perf:
                perf[name] = []
            perf[name].append(score)
            if len(perf[name]) > 50:
                perf[name] = perf[name][-50:]


def _predicted_win_pct(signals: dict, weights: dict, lifetime_wr: dict,
                        direction: int = 0) -> float:
    """Weighted avg lifetime win rate of strategies that agreed on direction."""
    from backtester.composite_scorer import _get_long_weight, _get_short_weight
    total_w = total_wr_w = 0.0
    for name, sig in signals.items():
        if sig.direction != direction:
            continue
        if direction == +1:
            w = _get_long_weight(weights, name)
        else:
            w = _get_short_weight(weights, name)
        total_w    += w
        total_wr_w += w * _get_wr(name, direction)
    return round(total_wr_w / total_w, 1) if total_w > 0 else 50.0


def _strategies_fired(signals: dict, direction: int) -> str:
    return ",".join(sorted(n for n, s in signals.items() if s.direction == direction))


# ─────────────────────────────────────────────────────────────────────────────
# PAPER TRADE LOG
# ─────────────────────────────────────────────────────────────────────────────

def _paper_result(exit_reason: str, pnl_rs: float) -> str:
    if exit_reason == "TARGET_HIT":
        return "EXACT_WIN"
    return "WIN" if pnl_rs > 0 else "LOSS"


def _append_paper_trade_log(day_result: dict) -> None:
    if not day_result["recommendations"]:
        return

    rows = []
    for rec in day_result["recommendations"]:
        sig     = rec["signal"]
        outcome = rec["outcome"]
        ep      = sig["entry"]
        xp      = outcome["exit_price"]
        pos_rs  = rec["position_rs"]
        pnl     = rec["pnl_rs"]

        rows.append({
            "date":             day_result["date"],
            "symbol":           rec["symbol"],
            "direction":        rec.get("direction", "LONG"),
            "signal_time":      sig["signal_time"],
            "entry_price":      ep,
            "quantity":         rec["shares"],
            "position_rs":      pos_rs,
            "stop_loss":        sig["stop"],
            "target":           sig["target"],
            "rr":               sig["rr"],
            "strategies_fired": rec.get("strategies_fired", ""),
            "agreeing_count":   rec["agreeing"],
            "composite_score":  rec["score"],
            "driver_strategy":  sig["strategy"],
            "reason":           sig.get("reason", ""),
            "exit_time":        outcome["exit_time"],
            "exit_price":       xp,
            "exit_reason":      outcome["exit_reason"],
            "result":           _paper_result(outcome["exit_reason"], pnl),
            "pnl_rs":           pnl,
            "pnl_pct":          round(pnl / pos_rs * 100, 2) if pos_rs else 0.0,
            "predicted_win_pct": rec.get("predicted_win_pct", 50.0),
            "conviction_tier":  rec.get("conviction_tier", "STANDARD"),
        })

    new_df = pd.DataFrame(rows)
    PAPER_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = not PAPER_TRADES_FILE.exists()
    new_df.to_csv(PAPER_TRADES_FILE, mode="a", header=header, index=False)


# ─────────────────────────────────────────────────────────────────────────────
# TRADE LOG
# ─────────────────────────────────────────────────────────────────────────────

def _append_trade_log(year: int, day_result: dict) -> None:
    if not day_result["recommendations"]:
        return

    rows = []
    for rec in day_result["recommendations"]:
        sig = rec["signal"]
        rows.append({
            "date":            day_result["date"],
            "symbol":          rec["symbol"],
            "direction":       rec.get("direction", "LONG"),
            "driver_strategy": sig["strategy"],
            "composite_score": rec["score"],
            "agreeing_count":  rec["agreeing"],
            "entry_time":      sig["signal_time"],
            "entry_price":     sig["entry"],
            "target":          sig["target"],
            "stop":            sig["stop"],
            "rr":              sig["rr"],
            "shares":          rec["shares"],
            "position_rs":     rec["position_rs"],
            "exit_price":      rec["outcome"]["exit_price"],
            "result":          rec["outcome"]["exit_reason"],
            "pnl_rs":          rec["pnl_rs"],
            "reason":          sig.get("reason", ""),
        })

    new_df  = pd.DataFrame(rows)
    log_dir = TRADE_LOG_DIR / str(year)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "trades.parquet"

    if log_path.exists():
        existing = pd.read_parquet(log_path)
        new_df = (pd.concat([existing, new_df])
                    .drop_duplicates(subset=["date", "symbol", "entry_time"])
                    .reset_index(drop=True))
    new_df.to_parquet(log_path, compression="snappy", index=False)


# ─────────────────────────────────────────────────────────────────────────────
# CHECKPOINT & MEMORY
# ─────────────────────────────────────────────────────────────────────────────

def _load_progress(prog_file: Path = PROGRESS_FILE) -> dict:
    if prog_file.exists():
        with open(prog_file) as f:
            return json.load(f)
    return {"download": {}, "analysis": {}}


def _save_day_checkpoint(progress, year_str, trade_date, days_since_update,
                          perf_long, perf_short,
                          prog_file: Path = PROGRESS_FILE,
                          perf_long_f: str = "strategy_performance_long.json",
                          perf_short_f: str = "strategy_performance_short.json"):
    if "analysis" not in progress:
        progress["analysis"] = {}
    progress["analysis"][year_str] = {
        "last_processed_date":    str(trade_date),
        "days_since_weight_update": days_since_update,
    }
    with open(prog_file, "w") as f:
        json.dump(progress, f, indent=2, default=str)
    _save_performance(perf_long,  perf_long_f)
    _save_performance(perf_short, perf_short_f)


def _save_performance(perf: dict, filename: str) -> None:
    path = PROGRESS_FILE.parent / filename
    with open(path, "w") as f:
        json.dump(perf, f, indent=2)


def _load_performance(filename: str) -> dict:
    path = PROGRESS_FILE.parent / filename
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def _load_weights(wf_weights_file: Path | None = None) -> dict:
    path = wf_weights_file if wf_weights_file else WEIGHTS_FILE
    if path and path.exists():
        with open(path) as f:
            raw = json.load(f)
        # Migrate any plain-float values to dict format
        migrated = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                migrated[k] = v
            else:
                migrated[k] = {"long": float(v), "short": 1.0}
        return migrated
    # Default: all strategies start at weight 1.0 for both directions
    return {name: {"long": 1.0, "short": 1.0} for name in STRATEGY_NAMES}


def _save_weights(weights: dict) -> None:
    WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)


def _year_summary(year, day_results, weights, perf_long, perf_short) -> dict:
    total_pnl    = sum(d["total_pnl"] for d in day_results)
    total_trades = sum(len(d["recommendations"]) for d in day_results)

    exact_wins = profitable_exits = losses = 0
    long_trades = short_trades = 0
    long_pnl = short_pnl = 0.0

    for d in day_results:
        for r in d["recommendations"]:
            result = r["outcome"]["exit_reason"]
            pnl    = r["pnl_rs"]
            dirn   = r.get("direction", "LONG")
            if dirn == "LONG":
                long_trades += 1
                long_pnl    += pnl
            else:
                short_trades += 1
                short_pnl    += pnl
            if result == "TARGET_HIT":
                exact_wins += 1
            elif result == "TIME_EXIT" and pnl > 0:
                profitable_exits += 1
            else:
                losses += 1

    profitable_trades = exact_wins + profitable_exits

    # Per-strategy stats with long/short signal split
    strategy_stats = {}
    for name in set(list(perf_long.keys()) + list(perf_short.keys())):
        long_out  = perf_long.get(name, [])
        short_out = perf_short.get(name, [])
        combined  = long_out + short_out
        if combined:
            wr = round(sum(combined) / len(combined) * 100, 1)
            strategy_stats[name] = {
                "win_rate":      wr,
                "signals":       len(combined),
                "long_signals":  len(long_out),
                "short_signals": len(short_out),
            }

    return {
        "year":               year,
        "total_pnl":          total_pnl,
        "total_trades":       total_trades,
        "long_trades":        long_trades,
        "short_trades":       short_trades,
        "long_pnl":           long_pnl,
        "short_pnl":          short_pnl,
        "exact_wins":         exact_wins,
        "profitable_exits":   profitable_exits,
        "losses":             losses,
        "effective_win_rate": round(profitable_trades / total_trades * 100, 1) if total_trades else 0,
        "exact_win_rate":     round(exact_wins / total_trades * 100, 1) if total_trades else 0,
        "final_weights":      weights,
        "strategy_stats":     strategy_stats,
    }


def _write_year_summary(year, summary) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = MEMORY_DIR / "strategy_agent.md"

    t    = summary["total_trades"]
    lt   = summary["long_trades"]
    st   = summary["short_trades"]
    ew   = summary["exact_wins"]
    pe   = summary["profitable_exits"]
    lo   = summary["losses"]
    lpnl = summary["long_pnl"]
    spnl = summary["short_pnl"]

    # Strategy performance table (long weights)
    perf_lines = []
    stats   = summary.get("strategy_stats", {})
    weights = summary["final_weights"]
    for name in sorted(weights, key=lambda n: -(weights[n].get("long", 1.0)
                                                if isinstance(weights[n], dict)
                                                else weights[n])):
        w = weights[name]
        wl = w.get("long", 1.0)  if isinstance(w, dict) else float(w)
        ws = w.get("short", 1.0) if isinstance(w, dict) else 1.0
        if name in stats:
            wr  = stats[name]["win_rate"]
            lng = stats[name]["long_signals"]
            srt = stats[name]["short_signals"]
            verdict = ("BEST" if wl >= 2.0 else
                       "OK"   if wl >= 1.0 else
                       "SUPPRESSED" if wl <= 0.1 else "REDUCED")
            perf_lines.append(
                f"| {name:<18} | {wl:.2f} | {ws:.2f} | {wr:>5.1f}% | {lng:>4} | {srt:>4} | {verdict} |"
            )
        else:
            perf_lines.append(
                f"| {name:<18} | {wl:.2f} | {ws:.2f} |   n/a |    0 |    0 | NO SIGNALS |"
            )

    perf_table = (
        "| Strategy           | wt_long | wt_short |  Win%  | L_Sig | S_Sig | Verdict    |\n"
        "|--------------------|---------|----------|--------|-------|-------|------------|\n"
        + "\n".join(perf_lines)
    )

    entry = (
        f"\n## Year {year} Summary\n"
        f"- Total trades        : {t} ({lt} LONG, {st} SHORT)\n"
        f"- Exact target hits   : {ew} ({summary['exact_win_rate']}%)  — price reached target\n"
        f"- Profitable exits    : {pe} ({round(pe/t*100,1) if t else 0}%)  — TIME_EXIT with positive P&L\n"
        f"- Losses              : {lo} ({round(lo/t*100,1) if t else 0}%)  — stopped out or negative exit\n"
        f"- Effective win rate  : {summary['effective_win_rate']}%\n"
        f"- Total P&L           : Rs {summary['total_pnl']:,.0f}  "
        f"(Long Rs {lpnl:,.0f} | Short Rs {spnl:,.0f})\n\n"
        f"### Strategy Performance — {year}\n"
        f"{perf_table}\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
