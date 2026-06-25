"""
Live Paper Trading Agent — Phase 2.5

Daily usage (run before 9:15 AM):
    python live/agent.py --token YOUR_KITE_ACCESS_TOKEN

What it does each day:
  PRE-MARKET  (9:00 – 9:14 AM)
    1. Authenticate with Kite using today's access token
    2. Fetch NSE instrument list → build symbol-to-token map
    3. Run pre-market filter on parquet history → top 25 watchlist stocks
    4. Load 30-day 5-min history from parquet for watchlist stocks
    5. Connect KiteTicker, subscribe all 25 tokens + Nifty50
    6. Wait for 9:15 AM market open

  MARKET HOURS (9:15 AM – 2:00 PM)
    Every 5-min bar close:
      - close_bar() on all candle builders
      - run scan_once() across watchlist
      - If a recommendation passes all filters → log entry, monitor for exit

  EXIT MONITORING (after trade is placed → 3:15 PM)
    - KiteTicker on_ticks() checks live price against target / stop
    - TARGET_HIT or STOP_HIT → log closed trade immediately
    - 3:15 PM TIME_EXIT → log at last traded price

  POST-MARKET (3:15 PM)
    - Print day summary
    - Disconnect KiteTicker
    - Exit

Crash safety:
  - Open trade state is checkpointed to checkpoints/live_open_trade.json
  - Restart the agent with the same token — it resumes monitoring the open trade

How to get the access token:
  1. Run: python -c "from data_pipeline.kite_auth import login_step1; login_step1()"
  2. Open the printed URL, log in to Zerodha
  3. Copy the request_token from the redirect URL
  4. Run: python -c "from data_pipeline.kite_auth import login_step2; login_step2('YOUR_REQUEST_TOKEN')"
  5. Find your access_token in checkpoints/access_token.json
  OR simply pass --token directly if you already have today's access token.
"""

import argparse
import json
import logging
import threading
import time as time_mod
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path

import pytz

import pandas as pd

_IST = pytz.timezone("Asia/Kolkata")

def _to_ist(series: pd.Series) -> pd.Series:
    """Convert a datetime Series to IST-naive (09:15, not 03:45).
    Matches CandleBuilder bar labels so history concat stays in order."""
    dt = pd.to_datetime(series)
    if dt.dt.tz is not None:
        return dt.dt.tz_convert(_IST).dt.tz_localize(None)
    return dt

# kiteconnect imported lazily inside main() — importing it at module level can
# hang on Python 3.13 due to SSL/WebSocket initialisation during import.
# from kiteconnect import KiteConnect, KiteTicker  ← moved to main()

from config.settings import WEIGHTS_FILE
from live.instrument_map import load_instrument_map, NIFTY50_TOKEN
from live.data_manager import LiveDataManager
from live.live_engine import scan_once
from live.paper_logger import (
    save_open_trade, load_open_trade, log_closed_trade
)
from live.risk_guard import check_risk_limits, write_eod_risk_check
from watchlist.pre_filter import PreMarketFilter

# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

IST         = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
NO_ENTRY    = dtime(14, 0)    # no new positions after 2:00 PM
SQUARE_OFF  = dtime(15, 15)   # force exit at 3:15 PM


# ─────────────────────────────────────────────────────────────────────────────
# Agent state (shared between main thread and KiteTicker callback thread)
# ─────────────────────────────────────────────────────────────────────────────

class AgentState:
    """Tracks up to one open LONG and one open SHORT position per day."""

    def __init__(self):
        self._long:         dict | None = None   # None = no LONG placed or already closed
        self._short:        dict | None = None   # None = no SHORT placed or already closed
        self._long_placed:  bool        = False  # True once any LONG was placed today
        self._short_placed: bool        = False  # True once any SHORT was placed today
        self._lock = threading.Lock()

    def set_long(self, rec: dict) -> None:
        with self._lock:
            self._long        = rec
            self._long_placed = True

    def set_short(self, rec: dict) -> None:
        with self._lock:
            self._short        = rec
            self._short_placed = True

    def close_long(self) -> None:
        with self._lock:
            self._long = None

    def close_short(self) -> None:
        with self._lock:
            self._short = None

    def is_long_open(self) -> bool:
        with self._lock:
            return self._long is not None

    def is_short_open(self) -> bool:
        with self._lock:
            return self._short is not None

    def snapshot(self) -> tuple[dict | None, dict | None, bool, bool]:
        """Return (long_rec, short_rec, long_placed, short_placed) atomically."""
        with self._lock:
            return self._long, self._short, self._long_placed, self._short_placed


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = _parse_args()
    today = date.today()

    log.info("=" * 65)
    log.info(f"  Live Paper Trading Agent — Phase 2.5 — {today}")
    log.info("=" * 65)

    # ── 1. Authenticate ─────────────────────────────────────────────────────
    # Broker is imported lazily to avoid kiteconnect's SSL/WebSocket init hang.
    from brokers import get_broker  # noqa: PLC0415
    broker = get_broker(args.broker)
    log.info(f"Loading {broker.display_name} API classes...")
    KiteConnect, KiteTicker = broker.get_api_classes()

    api_key, access_token = broker.get_credentials(args)
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    log.info(f"{broker.display_name} authenticated successfully")

    # ── 2. Instrument map ───────────────────────────────────────────────────
    imap = load_instrument_map(kite, args.broker)

    # ── 2b. Gap-fill — download any missed EOD data before loading history ──
    # If yesterday's EOD was not downloaded (e.g. ran Groww but EOD was
    # Zerodha-only, or agent was stopped before 15:31), fill the gap now
    # using the active broker.  Returns instantly when parquets are current.
    _fill_parquet_gaps(kite, imap, today)

    # ── 3. Load frozen WF weights ────────────────────────────────────────────
    weights = _load_weights(args.wf_window)
    log.info(f"Loaded WF{args.wf_window} frozen weights ({len(weights)} strategies)")

    # ── 3b. Risk limits check — exits if monthly/daily halt flag is active ──
    check_risk_limits(today)

    # ── 4. Load history + pre-market filter → watchlist ────────────────────
    # Load parquet history for ALL universe stocks — needed so the pre-market filter
    # can score the full 500-stock universe and return the top 25.
    universe_symbols = _load_universe_symbols()
    log.info(f"Universe: {len(universe_symbols)} stocks — loading parquet history...")
    all_history = _load_history_for_symbols(universe_symbols, today)
    log.info(f"History loaded for {len(all_history)} symbols")

    # Run pre-market filter → top 25 watchlist
    pre_filter  = PreMarketFilter()
    watchlist   = pre_filter.build(today, all_history)
    wl_symbols  = [e["symbol"] for e in watchlist]
    log.info(f"Watchlist ({len(wl_symbols)}): {', '.join(wl_symbols)}")

    # Save full watchlist so test_single_day and post-market review can load it
    from watchlist.pre_filter import save_watchlist
    save_watchlist(watchlist, today)

    # ── 5. Data manager — reuse already-loaded history, don't reload from parquet ──
    dm = LiveDataManager(symbols=wl_symbols, imap=imap)
    for sym in wl_symbols:
        if sym in all_history:
            dm._history[sym] = all_history[sym]
    dm._load_nifty_history([today.year - 1, today.year], today)
    # Release non-watchlist history to free memory
    del all_history

    # ── 5b. Resume today's candle bars if agent was restarted mid-day ────────
    dm.resume_from_checkpoint(today)

    # ── 5c. Late start: backfill today's bars from Kite API if market is open ─
    # Runs when the agent starts (or restarts) after 9:20 AM and candle history
    # is incomplete — either no checkpoint (fresh late start) or checkpoint only
    # has bars from a mid-day session (not from 9:15 AM).
    now_ist = _now_ist()
    if now_ist.time() > dtime(9, 20):
        # How many 5-min bars should have closed since 9:15 AM?
        market_open_dt = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        last_closed_t  = now_ist.replace(
            minute=(now_ist.minute // 5) * 5, second=0, microsecond=0
        ) - timedelta(minutes=5)
        expected_bars = max(0, int((last_closed_t - market_open_dt).total_seconds() / 300) + 1)

        # How many bars do we actually have? (use the most-populated symbol)
        actual_bars = max(
            (len(dm._builders[s].closed_bars) for s in dm.symbols[:5]
             if dm._builders[s].closed_bars),
            default=0,
        )

        if expected_bars > 0 and actual_bars < expected_bars * 0.8:
            log.info(
                f"Candle history incomplete: {actual_bars} bars vs {expected_bars} expected "
                f"— backfilling from Kite API"
            )
            _backfill_today_bars(kite, dm, today)

    # ── 6. Check if there's a crash-recovered open trade from today ─────────
    state = AgentState()
    _, long_rec, short_rec = load_open_trade()
    if long_rec:
        state.set_long(long_rec)
        log.info(f"RECOVERED open LONG:  {long_rec['symbol']} — monitoring for exit")
    if short_rec:
        state.set_short(short_rec)
        log.info(f"RECOVERED open SHORT: {short_rec['symbol']} — monitoring for exit")

    # ── 7. Ticker WebSocket setup ────────────────────────────────────────────
    tokens     = dm.instrument_tokens
    _last_tick = [None]   # [datetime | None] — updated on every tick; mutable so closure can write
    _ws_holder = [None]   # [current ws] — swapped on reconnect

    log.info(f"Subscribing {len(tokens)} instrument tokens to {broker.display_name} ticker")

    def _make_ticker():
        """Create, wire, and start a fresh ticker. Called at startup and on reconnect."""
        try:
            # Zerodha KiteTicker supports reconnect=True natively (default 50 retries).
            # Groww GrowwTickerAdapter ignores unknown kwargs — safe to pass.
            new_ws = KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=50)
        except TypeError:
            new_ws = KiteTicker(api_key, access_token)

        def on_connect(ws, _response):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            log.info(f"{broker.display_name} ticker connected and subscribed")

        def on_ticks(_ws, ticks):
            _last_tick[0] = datetime.now()
            for tick in ticks:
                dm.on_tick(tick)
            if state.is_long_open() or state.is_short_open():
                _check_exit(state, dm, today)

        def on_close(_ws, code, reason):
            log.warning(f"Ticker disconnected: {code} {reason}")

        def on_error(_ws, code, reason):
            log.error(f"Ticker error: {code} {reason}")

        new_ws.on_connect = on_connect
        new_ws.on_ticks   = on_ticks
        new_ws.on_close   = on_close
        new_ws.on_error   = on_error
        new_ws.connect(threaded=True)
        return new_ws

    _ws_holder[0] = _make_ticker()

    log.info("Waiting for market open (9:15 AM IST)...")

    # ── 8. Main scheduling loop ──────────────────────────────────────────────
    try:
        _run_market_loop(_ws_holder, _last_tick, _make_ticker, dm, state, weights, today)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        if _ws_holder[0]:
            _ws_holder[0].close()
        log.info("Ticker disconnected. Agent stopped.")

    # ── 9. Day summary ───────────────────────────────────────────────────────
    _print_summary(state, today)

    # ── 10. Post-market EOD data download ────────────────────────────────────
    _run_eod_download(kite, imap, today)


# ─────────────────────────────────────────────────────────────────────────────
# Market loop — runs in main thread
# ─────────────────────────────────────────────────────────────────────────────

_TICKER_STALE_SECS = 300   # warn + reconnect if no tick for 5 minutes during market hours

def _run_market_loop(ws_holder: list, last_tick: list, make_ticker,
                     dm: LiveDataManager, state: AgentState,
                     weights: dict, today: date) -> None:
    """
    Main scheduling loop.
    Sleeps until the next 5-min bar boundary, then:
      - Closes the bar on all candle builders
      - If no trade yet and time < 2 PM: runs scan_once()
      - If signal found: saves and monitors it
      - Checks ticker staleness — reconnects if no tick for 5+ minutes
      - At 3:15 PM: forces TIME_EXIT and exits loop
    """
    while True:
        now     = _now_ist()
        now_t   = now.time()

        # Post-market — done
        if now_t >= SQUARE_OFF:
            _force_time_exit(state, dm, today, now_t.strftime("%H:%M"))
            break

        # Wait for next 5-min bar boundary
        next_bar_dt = _next_bar_close(now)
        sleep_secs  = (next_bar_dt - now).total_seconds() + 0.3  # 0.3s grace — enough for last ticks, avoids bleeding next bar data

        if sleep_secs > 0:
            time_mod.sleep(sleep_secs)

        # Recalculate now after sleep
        now   = _now_ist()
        now_t = now.time()

        if now_t < MARKET_OPEN:
            continue   # still pre-market, keep waiting

        # ── Ticker staleness check ───────────────────────────────────────────
        # If no tick has arrived for 5+ minutes during market hours, the
        # WebSocket has silently died. Close and restart a fresh connection.
        if MARKET_OPEN <= now_t < SQUARE_OFF and last_tick[0] is not None:
            age = (datetime.now() - last_tick[0]).total_seconds()
            if age > _TICKER_STALE_SECS:
                log.warning(
                    f"Ticker stale: no ticks for {age:.0f}s — reconnecting..."
                )
                try:
                    ws_holder[0].close()
                except Exception:
                    pass
                time_mod.sleep(2)
                try:
                    ws_holder[0] = make_ticker()
                    last_tick[0] = datetime.now()   # reset clock; on_ticks will update
                    log.info("Ticker reconnected successfully")
                except Exception as e:
                    log.error(f"Ticker reconnect failed: {e} — will retry next bar")

        # Close the completed 5-min bar
        bar_label = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
        bar_label = bar_label.replace(tzinfo=None)  # strip tz for DataFrame compatibility
        if bar_label.time() < MARKET_OPEN:
            continue  # first wake at 09:15 gives bar_label=09:10 (pre-open) — skip it
        dm.close_bar(bar_label)
        log.debug(f"Bar closed: {bar_label.strftime('%H:%M')}")

        # Scan per-direction — one trade per direction per day (matches backtester rule)
        long_snap, short_snap, long_placed, short_placed = state.snapshot()
        need_long  = long_snap  is None and not long_placed  and now_t < NO_ENTRY
        need_short = short_snap is None and not short_placed and now_t < NO_ENTRY

        if need_long or need_short:
            recs = scan_once(dm, weights, today,
                             want_long=need_long, want_short=need_short)
            placed_any = False
            for rec in recs:
                dirn = rec["direction"]
                if dirn == "LONG" and not state.is_long_open():
                    state.set_long(rec)
                    placed_any = True
                    log.info(
                        f"PAPER TRADE PLACED [LONG]:  {rec['symbol']} | "
                        f"signal_time={rec['signal']['signal_time']} "
                        f"entry_time={rec.get('entry_time', now_t.strftime('%H:%M'))} | "
                        f"entry={rec['signal']['entry']:.2f} "
                        f"target={rec['signal']['target']:.2f} "
                        f"stop={rec['signal']['stop']:.2f} | "
                        f"size=Rs {rec['position_rs']:,.0f} ({rec['shares']} shares)"
                    )
                elif dirn == "SHORT" and not state.is_short_open():
                    state.set_short(rec)
                    placed_any = True
                    log.info(
                        f"PAPER TRADE PLACED [SHORT]: {rec['symbol']} | "
                        f"signal_time={rec['signal']['signal_time']} "
                        f"entry_time={rec.get('entry_time', now_t.strftime('%H:%M'))} | "
                        f"entry={rec['signal']['entry']:.2f} "
                        f"target={rec['signal']['target']:.2f} "
                        f"stop={rec['signal']['stop']:.2f} | "
                        f"size=Rs {rec['position_rs']:,.0f} ({rec['shares']} shares)"
                    )
            if placed_any:
                long_rec, short_rec, _, _ = state.snapshot()
                save_open_trade(today, long_rec, short_rec)
            else:
                dirs = []
                if need_long:  dirs.append("LONG")
                if need_short: dirs.append("SHORT")
                log.info(f"  {now_t.strftime('%H:%M')} — no signal for {'/'.join(dirs)}")

        if state.is_long_open() or state.is_short_open():
            _log_trade_monitor(state, dm)

        # Intraday 3:15 PM check
        if now_t >= SQUARE_OFF:
            _force_time_exit(state, dm, today, now_t.strftime("%H:%M"))
            break


# ─────────────────────────────────────────────────────────────────────────────
# Exit monitoring
# ─────────────────────────────────────────────────────────────────────────────

_monitor_last_seen: dict[str, tuple[float, int]] = {}  # symbol -> (price, consecutive_unchanged_bars)

def _log_trade_monitor(state: AgentState, dm: LiveDataManager) -> None:
    """Log current price and unrealised P&L every 5-min bar for each open position."""
    long_rec, short_rec, _, _ = state.snapshot()
    for rec in (long_rec, short_rec):
        if rec is None:
            continue
        symbol     = rec["symbol"]
        direction  = rec.get("direction", "LONG")
        last_price = dm.get_last_price(symbol)
        if last_price is None:
            continue
        entry  = float(rec["signal"]["entry"])
        target = float(rec["signal"]["target"])
        stop   = float(rec["signal"]["stop"])
        shares = rec["shares"]

        # Staleness detector: warn if price unchanged for 2+ consecutive bars
        prev_price, stale_count = _monitor_last_seen.get(symbol, (None, 0))
        if prev_price is not None and last_price == prev_price:
            stale_count += 1
        else:
            stale_count = 0
        _monitor_last_seen[symbol] = (last_price, stale_count)
        stale_tag = f"  *** STALE TICK — price unchanged for {stale_count + 1} bars ***" if stale_count >= 1 else ""

        if direction == "LONG":
            pnl       = round((last_price - entry) * shares, 2)
            pnl_pct   = round((last_price - entry) / entry * 100, 2)
            to_target = round((target - last_price) / last_price * 100, 2)
            to_stop   = round((last_price - stop)   / last_price * 100, 2)
        else:
            pnl       = round((entry - last_price) * shares, 2)
            pnl_pct   = round((entry - last_price) / entry * 100, 2)
            to_target = round((last_price - target) / last_price * 100, 2)
            to_stop   = round((stop - last_price)   / last_price * 100, 2)

        log.info(
            f"  MONITOR [{direction}] {symbol} @ {last_price:.2f} | "
            f"P&L Rs {pnl:+,.0f} ({pnl_pct:+.2f}%) | "
            f"to_target={to_target:+.2f}% to_stop={to_stop:+.2f}%{stale_tag}"
        )
        if stale_count >= 1:
            log.warning(f"  STALE TICK [{symbol}]: WebSocket may have dropped — exit checks unreliable")


def _check_exit(state: AgentState, dm: LiveDataManager, today: date) -> None:
    """
    Called on every tick (from broker WebSocket callback).
    Checks target/stop independently for the open LONG and open SHORT position.
    """
    now_str = _now_ist().strftime("%H:%M")
    changed = False
    long_rec, short_rec, _, _ = state.snapshot()

    for rec, close_fn in ((long_rec, state.close_long), (short_rec, state.close_short)):
        if rec is None:
            continue
        symbol    = rec["symbol"]
        direction = rec.get("direction", "LONG")
        target    = float(rec["signal"]["target"])
        stop      = float(rec["signal"]["stop"])
        last_price = dm.get_last_price(symbol)
        if last_price is None:
            continue

        if direction == "LONG":
            target_hit = last_price >= target
            stop_hit   = last_price <= stop
        else:
            target_hit = last_price <= target
            stop_hit   = last_price >= stop

        if target_hit:
            log.info(f"TARGET HIT [{direction}]: {symbol} @ {last_price:.2f} (target={target:.2f})")
            close_fn()
            log_closed_trade(today, rec, exit_price=target, exit_reason="TARGET_HIT", exit_time=now_str)
            changed = True
        elif stop_hit:
            log.info(f"STOP HIT [{direction}]: {symbol} @ {last_price:.2f} (stop={stop:.2f})")
            close_fn()
            log_closed_trade(today, rec, exit_price=stop, exit_reason="STOP_HIT", exit_time=now_str)
            changed = True

    if changed:
        new_long, new_short, _, _ = state.snapshot()
        save_open_trade(today, new_long, new_short)


def _force_time_exit(state: AgentState, dm: LiveDataManager,
                     today: date, exit_time: str) -> None:
    """Force-close all open positions at 3:15 PM."""
    long_rec, short_rec, long_placed, short_placed = state.snapshot()
    if not long_placed and not short_placed:
        log.info("No trade was placed today — no exit needed")
        return

    for rec, close_fn in ((long_rec, state.close_long), (short_rec, state.close_short)):
        if rec is None:
            continue
        symbol    = rec["symbol"]
        direction = rec.get("direction", "LONG")
        last_price = dm.get_last_price(symbol)
        if last_price is None:
            last_price = float(rec["signal"]["entry"])
            log.warning(f"  No live price for {symbol} — using entry price as exit")
        log.info(f"TIME EXIT [{direction}]: {symbol} @ {last_price:.2f} (3:15 PM square-off)")
        close_fn()
        log_closed_trade(today, rec, exit_price=last_price, exit_reason="TIME_EXIT", exit_time=exit_time)

    save_open_trade(today, None, None)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    return datetime.now(IST)


def _next_bar_close(now: datetime) -> datetime:
    """
    Return the datetime of the next 5-min bar close.
    NSE bars: 9:15, 9:20, 9:25, ..., 15:25 (labelled by bar open time).
    A bar "closes" when the next bar starts, i.e. at :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55.

    We treat bars as closing at multiples of 5 minutes past the hour.
    """
    minute  = now.minute
    seconds = now.second + now.microsecond / 1e6
    # Minutes until next 5-min boundary
    offset  = 5 - (minute % 5)
    if offset == 5 and seconds < 1:
        offset = 5   # we're exactly on the mark — wait for next bar
    next_dt = now.replace(second=0, microsecond=0) + timedelta(minutes=offset)
    return next_dt


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live Paper Trading Agent — Phase 2.5")
    p.add_argument(
        "--token", metavar="ACCESS_TOKEN",
        help="Broker access token for today"
    )
    p.add_argument(
        "--broker", default="zerodha",
        help="Broker to use: zerodha (default) or groww"
    )
    p.add_argument(
        "--wf-window", type=int, default=5, metavar="N",
        help="Walk-forward window whose frozen weights to use (default: 5 → wf5_weights.json)"
    )
    return p.parse_args()


def _load_weights(wf_window: int = 5) -> dict:
    """Load frozen WF weights. Never loads strategy_weights.json (adaptive training file)."""
    frozen = WEIGHTS_FILE.parent / f"wf{wf_window}_weights.json"
    if frozen.exists():
        with open(frozen) as f:
            return json.load(f)
    log.warning(f"wf{wf_window}_weights.json not found — falling back to strategy_weights.json")
    if WEIGHTS_FILE.exists():
        with open(WEIGHTS_FILE) as f:
            return json.load(f)
    log.warning("No weights file found — using uniform weights (1.0 per strategy)")
    return {}


def _load_universe_symbols() -> list[str]:
    """Load tradingsymbols from config/universe.csv."""
    from config.settings import UNIVERSE_FILE
    import pandas as pd
    if not UNIVERSE_FILE.exists():
        log.warning("universe.csv not found — will scan all stocks with parquet data")
        return []
    df = pd.read_csv(UNIVERSE_FILE)
    col = "tradingsymbol" if "tradingsymbol" in df.columns else df.columns[0]
    return df[col].tolist()


def _load_history_for_symbols(symbols: list[str], today: date) -> dict:
    """Load parquet history for all symbols. Logs progress every 100 stocks."""
    import pandas as pd
    from config.settings import STOCKS_DIR

    result   = {}
    curr_yr  = today.year
    prev_yr  = curr_yr - 1
    total    = len(symbols)

    for i, symbol in enumerate(symbols):
        if i > 0 and i % 100 == 0:
            log.info(f"  Loading history... {i}/{total} stocks done")

        dfs = []
        for yr in [prev_yr, curr_yr]:
            path = STOCKS_DIR / str(yr) / f"{symbol}.parquet"
            if path.exists():
                try:
                    df = pd.read_parquet(path)
                    df["datetime"] = _to_ist(df["datetime"])
                    dfs.append(df)
                except Exception:
                    pass
        if dfs:
            combined = (pd.concat(dfs)
                        .drop_duplicates("datetime")
                        .sort_values("datetime")
                        .reset_index(drop=True))
            history = combined[combined["datetime"].dt.date < today]
            if not history.empty:
                result[symbol] = history.reset_index(drop=True)

    return result


def _backfill_today_bars(kite, dm: LiveDataManager, today: date) -> None:
    """
    Fetch today's completed 5-min bars from Kite historical API and seed the
    candle builders.  Called when the agent starts AFTER market open with no
    checkpoint data (late start or first-ever run mid-day).

    Without this, strategies only see bars captured since the agent started —
    missing all price action from 9:15 AM up to the actual start time.
    """
    now_ist = _now_ist()

    # Last fully-closed bar open time  (e.g. at 12:03 PM → last closed bar = 11:55)
    m = now_ist.minute
    last_closed = now_ist.replace(minute=m - (m % 5), second=0, microsecond=0) - timedelta(minutes=5)
    from_dt = now_ist.replace(hour=9, minute=15, second=0, microsecond=0).replace(tzinfo=None)
    to_dt   = last_closed.replace(tzinfo=None)

    if to_dt < from_dt:
        log.info("Late-start backfill: market not open long enough yet — skipping")
        return

    log.info(f"Late-start backfill: fetching today's bars 09:15 → {to_dt.strftime('%H:%M')} from Kite API...")

    def _fetch(token: int) -> list[dict]:
        bars = kite.historical_data(
            instrument_token=token,
            from_date=from_dt,
            to_date=to_dt,
            interval="5minute",
            continuous=False,
            oi=False,
        )
        result = []
        for b in bars:
            dt = b["date"]
            if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            result.append({
                "datetime": dt,
                "open":   float(b["open"]),
                "high":   float(b["high"]),
                "low":    float(b["low"]),
                "close":  float(b["close"]),
                "volume": int(b["volume"]),
            })
        return result

    filled = 0
    for i, symbol in enumerate(dm.symbols):
        token = dm._imap.get(symbol)
        if not token:
            continue
        try:
            bars = _fetch(token)
            if bars:
                dm._builders[symbol].seed(bars)
                filled += 1
        except Exception as e:
            log.debug(f"  {symbol}: backfill failed — {e}")
        # Kite allows 3 historical requests/sec
        if (i + 1) % 3 == 0:
            time_mod.sleep(1.0)

    # Backfill Nifty too (needed by REL-STR strategy)
    try:
        nifty_bars = _fetch(NIFTY50_TOKEN)
        if nifty_bars:
            dm._nifty_builder.seed(nifty_bars)
    except Exception as e:
        log.debug(f"  NIFTY50: backfill failed — {e}")

    log.info(f"Late-start backfill complete: {filled}/{len(dm.symbols)} symbols, "
             f"{len(bars) if filled else 0} bars each")


def _print_summary(state: AgentState, today: date) -> None:
    long_rec, short_rec, long_placed, short_placed = state.snapshot()
    log.info("")
    log.info("=" * 65)
    log.info(f"  Day summary — {today}")
    if not long_placed and not short_placed:
        log.info("  No trade placed today (no signal passed all filters)")
    for rec, placed, still_open, label in (
        (long_rec,  long_placed,  state.is_long_open(),  "LONG"),
        (short_rec, short_placed, state.is_short_open(), "SHORT"),
    ):
        if not placed:
            continue
        if rec is None:
            log.info(f"  [{label}] trade placed and closed — see live_paper_trades.csv")
            continue
        sig = rec["signal"]
        log.info(f"  [{label}] {rec['symbol']} | driver={sig['strategy']} "
                 f"agreeing={rec['agreeing']} score={rec['score']:.2f}")
        log.info(f"    entry={sig['entry']:.2f}  target={sig['target']:.2f}  "
                 f"stop={sig['stop']:.2f}  size=Rs {rec['position_rs']:,.0f} [{rec['conviction_tier']}]")
        if still_open:
            log.info(f"    WARNING: position not closed — check live_open_trade.json")
        else:
            log.info(f"    Exit recorded — see live_paper_trades.csv")
    log.info("=" * 65)

    # EOD risk check — writes halt flags if daily/monthly limits breached
    write_eod_risk_check(today)


def _fill_parquet_gaps(kite, imap: dict, today: date) -> None:
    """
    Check all universe stocks for missing parquet dates and download them.
    Called at startup after instrument map is loaded, before history loads.
    Uses the active broker (Zerodha or Groww) so no cross-broker dependency.
    Returns immediately if all parquets are up to date (typical case).
    """
    from data_pipeline.downloader import fill_gaps
    try:
        result = fill_gaps(kite, imap, today)
        if result["filled"] > 0:
            log.info(
                f"Gap-fill complete: {result['filled']} stocks updated"
                + (f", {len(result['failed'])} failed — {result['failed']}" if result["failed"] else "")
            )
    except Exception as e:
        log.warning(f"Gap-fill failed (non-fatal): {e}")


def _run_eod_download(kite, imap: dict, today: date) -> None:
    """
    Download today's 5-min bars for all 500 universe stocks after market close.
    Waits until 15:31 IST if called early (e.g. manual stop before close).
    imap is passed to download_eod so Groww exchange_tokens are used when
    running with --broker groww (no dependency on Zerodha tokens from universe.csv).
    """
    from data_pipeline.downloader import download_eod

    # Wait until 15:31 so API has the full day's candles available
    now_ist = _now_ist()
    target  = now_ist.replace(hour=15, minute=31, second=0, microsecond=0)
    wait_s  = (target - now_ist).total_seconds()
    if wait_s > 0:
        log.info(f"EOD download: waiting {int(wait_s)}s until 15:31 for API data to settle...")
        time_mod.sleep(wait_s)

    log.info("=" * 65)
    log.info("  Starting EOD data download for tomorrow's session")
    log.info("=" * 65)
    try:
        result = download_eod(kite, today, imap=imap)
        log.info(
            f"  EOD download done — {result['completed']} stocks saved, "
            f"{result['failed']} failed. Parquet files updated for {today}."
        )
        log.info("  Tomorrow's agent will load today's bars as history automatically.")
    except Exception as e:
        log.error(f"  EOD download failed: {e}")
        log.error("  Run manually: python -c \"from data_pipeline.downloader import download_eod; ...\"")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
