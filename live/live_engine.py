"""
Live scan engine — same decision logic as backtester/engine.py _process_day()
but fed from LiveDataManager instead of preloaded parquet files.

Phase 2B: finds best LONG and best SHORT candidate, applies direction bias,
returns the winner. Imports the same private functions from engine.py so
the live system stays byte-for-byte identical to the tested backtester.

scan_once() returns the best recommendation dict (LONG or SHORT), or None.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
import pytz

_IST = pytz.timezone("Asia/Kolkata")

from backtester.engine import (
    _best_signal,
    _conviction_multiplier,
    _predicted_win_pct,
    _strategies_fired,
    _LIFETIME_WR,
    _get_week52_low,
    _recent_move,
)
from backtester.composite_scorer import (
    long_composite_score, short_composite_score,
    count_agreeing_filtered,
)
from backtester.quality_filter import passes_all_filters
from backtester.position_sizer import position_size
from config.settings import AGREEMENT_MIN_LIFETIME_WR, SHORT_ENABLED
from strategies import ALL_STRATEGIES
from weights.regime import get_regime_modifiers, get_direction_bias
from watchlist.pre_filter import PreMarketFilter
from live.data_manager import LiveDataManager

log = logging.getLogger(__name__)

_TOP_N = 50


def build_live_watchlist(data_manager: LiveDataManager, trade_date: date) -> set[str]:
    """
    Run pre-market filter once before 9:15 AM.

    Call this once per day (e.g. at 9:00 AM) before the first scan_once() call.
    Returns a set of symbols (100 bullish + 100 bearish candidates) for the
    intraday scan. Passing this set to scan_once() avoids scanning all 300+
    stocks on every 5-min candle — no intraday latency added.
    """
    all_data: dict = {}
    for symbol in data_manager.symbols:
        hist = data_manager.get_history(symbol)
        if hist is not None and not hist.empty:
            all_data[symbol] = hist

    pf        = PreMarketFilter()
    watchlist = pf.build(trade_date, all_data)
    symbols   = {e["symbol"] for e in watchlist}
    log.info(f"Live watchlist built: {len(symbols)} symbols for {trade_date}")
    return symbols


def scan_once(
    data_manager: LiveDataManager,
    weights: dict,
    trade_date: date,
    watchlist: set[str] | None = None,
    want_long: bool = True,
    want_short: bool = True,
) -> list[dict]:
    """
    Run one full scan across watchlist symbols using current candle state.

    watchlist: set of symbols from build_live_watchlist() — call that once
               before 9:15 AM and pass the result on every subsequent call.
               If None, falls back to scanning all data_manager.symbols.

    want_long / want_short: skip finding a candidate in that direction when the
               slot is already filled (position already open for that direction).

    Returns a list of 0-2 recommendation dicts — at most one LONG, one SHORT.
    Call this every time a 5-min bar closes.
    """
    if not want_long and not want_short:
        return []
    nifty_today  = data_manager.get_nifty_today()
    vix          = _estimate_vix()
    nifty_pct    = _estimate_nifty_pct(data_manager)
    regime_mods  = get_regime_modifiers(weights, vix=vix)
    long_bias, short_bias = get_direction_bias(vix, nifty_pct)

    stock_signals:      dict[str, dict]   = {}
    stock_long_scores:  dict[str, tuple]  = {}   # {symbol: (raw, adj)}
    stock_short_scores: dict[str, tuple]  = {}

    symbols_to_scan = watchlist if watchlist is not None else set(data_manager.symbols)
    for symbol in symbols_to_scan:
        today_5min   = data_manager.get_today(symbol)
        history_5min = data_manager.get_history(symbol)
        prev_day     = data_manager.get_prev_day(symbol)

        if today_5min.empty:
            log.debug(f"  {symbol}: 0 bars today — skipping")
            continue

        signals = {}
        for strategy in ALL_STRATEGIES:
            try:
                sig = strategy.generate_signal(
                    today_5min=today_5min,
                    history_5min=history_5min,
                    prev_day=prev_day,
                    nifty_today=nifty_today,
                    trade_date=trade_date,
                )
                signals[strategy.name] = sig
            except Exception as e:
                from strategies.base import Signal
                signals[strategy.name] = Signal(strategy=strategy.name, direction=0)
                log.debug(f"  {symbol}/{strategy.name}: signal error — {e}")

        stock_signals[symbol] = signals
        ls_raw = long_composite_score(signals, weights, regime_mods)
        ss_raw = short_composite_score(signals, weights, regime_mods)
        stock_long_scores[symbol]  = (ls_raw, ls_raw * long_bias)
        stock_short_scores[symbol] = (ss_raw, ss_raw * short_bias)

    zero_bar_stocks = [s for s in symbols_to_scan if data_manager.get_today(s).empty]
    if zero_bar_stocks:
        log.info(f"  !! {len(zero_bar_stocks)} watchlist stocks have 0 bars: "
                 f"{', '.join(zero_bar_stocks[:10])}")

    if not stock_signals:
        return []

    daily_turnover = _estimate_turnover(data_manager, symbols_to_scan)
    results: list[dict] = []

    # ── Find best LONG candidate ─────────────────────────────────────────────
    if want_long:
        sorted_longs = sorted(stock_long_scores.items(), key=lambda x: x[1][1], reverse=True)
        _log_top_candidates(sorted_longs, stock_signals, data_manager,
                            daily_turnover, weights, direction=+1, nifty_pct=nifty_pct)
        best_long = _find_live_candidate(
            direction=+1,
            sorted_stocks=sorted_longs,
            stock_signals=stock_signals,
            data_manager=data_manager,
            daily_turnover=daily_turnover,
            weights=weights,
            nifty_pct=nifty_pct,
            vix=vix,
        )
        if best_long:
            results.append(best_long)

    # ── Find best SHORT candidate ────────────────────────────────────────────
    if want_short and SHORT_ENABLED:
        sorted_shorts = sorted(stock_short_scores.items(), key=lambda x: x[1][1], reverse=True)
        _log_top_candidates(sorted_shorts, stock_signals, data_manager,
                            daily_turnover, weights, direction=-1, nifty_pct=nifty_pct)
        best_short = _find_live_candidate(
            direction=-1,
            sorted_stocks=sorted_shorts,
            stock_signals=stock_signals,
            data_manager=data_manager,
            daily_turnover=daily_turnover,
            weights=weights,
            nifty_pct=nifty_pct,
            vix=vix,
        )
        if best_short:
            results.append(best_short)

    return results


def _find_live_candidate(
    direction: int,
    sorted_stocks: list,
    stock_signals: dict,
    data_manager: LiveDataManager,
    daily_turnover: dict,
    weights: dict,
    nifty_pct: float,
    vix: float,
) -> dict | None:
    for symbol, (raw_score, adj_score) in sorted_stocks[:_TOP_N]:
        if raw_score <= 0:
            break

        signals  = stock_signals.get(symbol)
        if not signals:
            continue
        today_5m = data_manager.get_today(symbol)

        best_sig = _best_signal(signals, direction=direction)
        if best_sig is None:
            continue

        agreeing     = count_agreeing_filtered(signals, direction, _LIFETIME_WR,
                                               AGREEMENT_MIN_LIFETIME_WR)
        turnover     = daily_turnover.get(symbol, 0)
        pred_win_pct = _predicted_win_pct(signals, weights, _LIFETIME_WR, direction=direction)

        today        = date.today()
        week52_low   = _get_week52_low(data_manager.get_history(symbol), today) if direction == -1 else 0.0
        recent_3d_mv = _recent_move(data_manager.get_history(symbol), today, days=3) if direction == -1 else 0.0

        passes, reason = passes_all_filters(
            signal               = best_sig,
            today_5min           = today_5m,
            daily_turnover_crore = turnover,
            strategies_agreeing  = agreeing,
            composite_score      = raw_score,
            predicted_win_pct    = pred_win_pct,
            nifty_pct_change     = nifty_pct,
            week52_low           = week52_low,
            recent_3day_move     = recent_3d_mv,
        )

        if passes:
            now_ist      = datetime.now(_IST)
            live_price   = data_manager.get_last_price(symbol)
            strategy_entry = best_sig.entry

            if live_price and live_price > 0:
                stop_dist   = abs(best_sig.entry - best_sig.stop)
                target_dist = abs(best_sig.target - best_sig.entry)
                live_entry  = round(live_price, 2)

                if direction == +1:
                    live_stop   = round(live_entry - stop_dist, 2)
                    live_target = round(live_entry + target_dist, 2)
                    valid = live_stop < live_entry < live_target
                else:
                    live_stop   = round(live_entry + stop_dist, 2)
                    live_target = round(live_entry - target_dist, 2)
                    valid = live_target < live_entry < live_stop

                if not valid or stop_dist <= 0:
                    log.debug(f"  {symbol}: live-price adjusted signal invalid — skip")
                    continue

                # Guard: live price already past strategy target
                if direction == +1 and live_price >= best_sig.target:
                    log.debug(f"  {symbol}: signal stale (long) — live price past target")
                    continue
                if direction == -1 and live_price <= best_sig.target:
                    log.debug(f"  {symbol}: signal stale (short) — live price past target")
                    continue

                best_sig.entry  = live_entry
                best_sig.stop   = live_stop
                best_sig.target = live_target
                if direction == +1:
                    best_sig.rr = round((live_target - live_entry) / (live_entry - live_stop), 2)
                else:
                    best_sig.rr = round((live_entry - live_target) / (live_stop - live_entry), 2)

            conv_mult, conv_tier = _conviction_multiplier(best_sig.strategy, direction)
            rs_value, shares     = position_size(best_sig.entry, best_sig.stop, conv_mult)
            entry_time           = now_ist.strftime("%H:%M")
            drift_pct            = round((best_sig.entry - strategy_entry) / strategy_entry * 100, 2)

            dirn_str = "LONG" if direction == +1 else "SHORT"
            log.info(
                f"SIGNAL [{dirn_str}]: {symbol} | driver={best_sig.strategy} | "
                f"signal_time={best_sig.signal_time} entry_time={entry_time} | "
                f"entry={best_sig.entry:.2f} (drift={drift_pct:+.2f}%) | "
                f"target={best_sig.target:.2f} stop={best_sig.stop:.2f} RR={best_sig.rr:.2f} | "
                f"agreeing={agreeing} score={adj_score:.2f} pred={pred_win_pct:.1f}% | "
                f"size=Rs {rs_value:,.0f} ({shares} shares) [{conv_tier}]"
            )
            sig_dict                   = best_sig.to_dict()
            sig_dict["strategy_entry"] = strategy_entry
            return {
                "symbol":            symbol,
                "score":             round(adj_score, 3),
                "raw_score":         round(raw_score, 3),
                "direction":         dirn_str,
                "signal":            sig_dict,
                "entry_time":        entry_time,
                "agreeing":          agreeing,
                "position_rs":       rs_value,
                "shares":            shares,
                "predicted_win_pct": pred_win_pct,
                "conviction_tier":   conv_tier,
                "conviction_mult":   conv_mult,
                "strategies_fired":  _strategies_fired(signals, direction),
                "vix":               vix,
            }
        else:
            log.debug(f"  {symbol} [{('LONG' if direction==+1 else 'SHORT')}] filtered: {reason}")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_vix() -> float:
    return 15.0


def _estimate_nifty_pct(data_manager: LiveDataManager) -> float:
    """Estimate Nifty's opening change vs prior day close (available at 9:20 AM)."""
    try:
        nifty_today = data_manager.get_nifty_today()
        nifty_hist  = data_manager.get_nifty_history()
        if nifty_today.empty or nifty_hist is None or nifty_hist.empty:
            return 0.0
        prev_close  = float(nifty_hist.iloc[-1]["close"])
        today_open  = float(nifty_today.iloc[0]["open"])
        if prev_close <= 0:
            return 0.0
        return round((today_open - prev_close) / prev_close * 100, 3)
    except Exception:
        return 0.0


def _log_top_candidates(
    sorted_stocks: list,
    stock_signals: dict,
    data_manager: LiveDataManager,
    daily_turnover: dict,
    weights: dict,
    direction: int,
    nifty_pct: float = 0.0,
) -> None:
    dirn_str = "LONG" if direction == +1 else "SHORT"
    shown = 0
    for symbol, score_pair in sorted_stocks[:20]:
        raw_score = score_pair[0]
        if raw_score <= 0 or shown >= 3:
            break
        signals      = stock_signals[symbol]
        today_5m     = data_manager.get_today(symbol)
        agreeing     = count_agreeing_filtered(signals, direction, _LIFETIME_WR, AGREEMENT_MIN_LIFETIME_WR)
        turnover     = daily_turnover.get(symbol, 0)
        pred_win_pct = _predicted_win_pct(signals, weights, _LIFETIME_WR, direction=direction)
        best_sig     = _best_signal(signals, direction=direction)
        if best_sig is None:
            log.info(f"    [{dirn_str}] {symbol}: score={raw_score:.2f} — no valid signal")
        else:
            today        = date.today()
            week52_low   = _get_week52_low(data_manager.get_history(symbol), today) if direction == -1 else 0.0
            recent_3d_mv = _recent_move(data_manager.get_history(symbol), today, days=3) if direction == -1 else 0.0
            passes, reason = passes_all_filters(
                signal               = best_sig,
                today_5min           = today_5m,
                daily_turnover_crore = turnover,
                strategies_agreeing  = agreeing,
                composite_score      = raw_score,
                predicted_win_pct    = pred_win_pct,
                nifty_pct_change     = nifty_pct,
                week52_low           = week52_low,
                recent_3day_move     = recent_3d_mv,
            )
            n_bars = len(today_5m)
            if passes:
                log.info(f"    [{dirn_str}] {symbol}: score={raw_score:.2f} agreeing={agreeing} "
                         f"bars={n_bars} PASS")
            else:
                log.info(f"    [{dirn_str}] {symbol}: score={raw_score:.2f} agreeing={agreeing} "
                         f"bars={n_bars} FAIL — {reason}")
        shown += 1


def _estimate_turnover(data_manager: LiveDataManager,
                       symbols: set[str] | None = None) -> dict[str, float]:
    result: dict[str, float] = {}
    for symbol in (symbols if symbols is not None else data_manager.symbols):
        hist = data_manager.get_history(symbol)
        if hist is None or hist.empty:
            result[symbol] = 0
            continue
        recent = hist.tail(20 * 75)
        by_day = recent.groupby(recent["datetime"].dt.date).apply(
            lambda x: (x["close"] * x["volume"]).sum() / 1e7
        )
        result[symbol] = float(by_day.median()) if not by_day.empty else 0
    return result
