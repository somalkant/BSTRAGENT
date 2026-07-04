"""
Live data manager: history from parquet + today's candles from KiteTicker ticks.

History load (at startup, before market open):
  - Reads data/stocks/YEAR/SYMBOL.parquet for the last 2 years
  - Filters to dates BEFORE today → strategy warmup (EMA, volume avg, etc.)
  - No API calls needed — parquet files are kept current by the downloader

Live candles (intraday):
  - KiteTicker ticks are routed here via on_tick()
  - Each 5-min bar is closed by close_bar() called from the agent scheduler
  - today_df is passed to strategies as today_5min
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime

import pandas as pd
import pytz

from config.settings import STOCKS_DIR, INDEX_DIR
from live.candle_builder import CandleBuilder
from live.candle_checkpoint import save_candles, load_candles
from live.instrument_map import NIFTY50_TOKEN

_IST = pytz.timezone("Asia/Kolkata")
_MARKET_OPEN_T = dtime(9, 15)   # NSE regular session start


def _to_ist(series: pd.Series) -> pd.Series:
    """Convert parquet datetime column to IST-naive so it matches CandleBuilder bar labels."""
    dt = pd.to_datetime(series)
    if dt.dt.tz is not None:
        return dt.dt.tz_convert(_IST).dt.tz_localize(None)
    return dt

log = logging.getLogger(__name__)

_HISTORY_YEARS = 2    # load current year + previous year from parquet


class LiveDataManager:
    def __init__(self, symbols: list[str], imap: dict[str, int]):
        """
        symbols : list of trading symbols for the watchlist (e.g. ["RELIANCE", "INFY"])
        imap    : {symbol: instrument_token} from load_instrument_map()
        """
        self.symbols = symbols
        self._imap   = imap

        # Reverse map: instrument_token → symbol (for tick dispatch)
        self._token_to_sym: dict[int, str] = {
            imap[s]: s for s in symbols if s in imap
        }

        # Parquet history up to yesterday (loaded at startup)
        self._history: dict[str, pd.DataFrame] = {}

        # Live candle builders — one per symbol + one for Nifty
        self._builders: dict[str, CandleBuilder] = {
            s: CandleBuilder(s) for s in symbols
        }
        self._nifty_builder = CandleBuilder("NIFTY50")

    # ── startup ──────────────────────────────────────────────────────────────

    def load_history_from_parquet(self) -> None:
        """
        Load parquet history for all watchlist symbols.
        Filters to dates BEFORE today so strategies don't see today's data.
        """
        today = date.today()
        years = [today.year - 1, today.year]

        log.info(f"Loading parquet history for {len(self.symbols)} watchlist symbols...")
        loaded = 0

        for symbol in self.symbols:
            dfs = []
            for yr in years:
                path = STOCKS_DIR / str(yr) / f"{symbol}.parquet"
                if not path.exists():
                    continue
                try:
                    df = pd.read_parquet(path)
                    df["datetime"] = _to_ist(df["datetime"])
                    dfs.append(df)
                except Exception as e:
                    log.warning(f"  {symbol} {yr}: read failed — {e}")

            if not dfs:
                continue

            combined = (pd.concat(dfs)
                        .drop_duplicates("datetime")
                        .sort_values("datetime")
                        .reset_index(drop=True))

            # Only keep data strictly before today (no look-ahead into live session)
            history = combined[combined["datetime"].dt.date < today]
            if not history.empty:
                self._history[symbol] = history.reset_index(drop=True)
                loaded += 1

        log.info(f"  History loaded for {loaded}/{len(self.symbols)} symbols")
        self._load_nifty_history(years, today)

    def _load_nifty_history(self, years: list[int], today: date) -> None:
        dfs = []
        for yr in years:
            path = INDEX_DIR / str(yr) / "NIFTY50.parquet"
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
            self._nifty_history = combined[combined["datetime"].dt.date < today].reset_index(drop=True)
        else:
            self._nifty_history = pd.DataFrame()

    # ── tick routing (called from KiteTicker on_ticks callback) ─────────────

    def on_tick(self, tick: dict) -> None:
        """Route a single tick to the correct candle builder."""
        if datetime.now(_IST).time() < _MARKET_OPEN_T:
            return  # discard pre-open ticks (NSE pre-open 09:00–09:15)
        token      = tick.get("instrument_token")
        price      = float(tick.get("last_price", 0))
        # KiteConnect MODE_FULL uses "volume_traded"; fall back to "volume" for compatibility
        day_volume = int(tick.get("volume_traded") or tick.get("volume") or 0)

        if price <= 0:
            return

        if token == NIFTY50_TOKEN:
            self._nifty_builder.on_tick(price, day_volume)
            return

        symbol = self._token_to_sym.get(token)
        if symbol:
            self._builders[symbol].on_tick(price, day_volume)

    # ── bar close (called by agent scheduler every 5 minutes) ────────────────

    def close_bar(self, bar_dt) -> None:
        """Close the 5-min bar across all builders. Call at :00, :05, :10, ... minutes."""
        for builder in self._builders.values():
            builder.close_bar(bar_dt)
        self._nifty_builder.close_bar(bar_dt)
        # Persist bars so a restart can resume from this point
        save_candles(date.today(), {**self._builders, "NIFTY50": self._nifty_builder})

    def resume_from_checkpoint(self, today: date) -> None:
        """
        Seed candle builders with bars saved from a previous run today.
        Call at startup before the market loop — restores today's bars so
        restarting mid-day doesn't lose already-built candle history.
        """
        saved = load_candles(today)
        if not saved:
            return
        count = 0
        for symbol, bars in saved.items():
            if symbol == "NIFTY50":
                self._nifty_builder.seed(bars)
            elif symbol in self._builders:
                self._builders[symbol].seed(bars)
                count += 1
        log.info(
            f"Resumed candle history from checkpoint: {count} watchlist symbols "
            f"+ NIFTY50 ({len(next(iter(saved.values()), []))} bars each approx)"
        )

    # ── data accessors (called by live_engine.scan_once) ────────────────────

    def get_history(self, symbol: str) -> pd.DataFrame:
        return self._history.get(symbol, pd.DataFrame())

    def get_today(self, symbol: str) -> pd.DataFrame:
        return self._builders[symbol].today_df

    def get_prev_day(self, symbol: str) -> pd.Series | None:
        hist = self._history.get(symbol)
        if hist is None or hist.empty:
            return None
        daily = (hist.groupby(hist["datetime"].dt.date)
                 .agg(open=("open", "first"), high=("high", "max"),
                      low=("low", "min"),  close=("close", "last"),
                      volume=("volume", "sum")))
        return daily.iloc[-1] if not daily.empty else None

    def get_nifty_today(self) -> pd.DataFrame:
        return self._nifty_builder.today_df

    def get_last_price(self, symbol: str) -> float | None:
        """Most recent tick price for an open position (used for exit monitoring)."""
        return self._builders[symbol].last_price

    @property
    def instrument_tokens(self) -> list[int]:
        """All instrument tokens to subscribe in KiteTicker."""
        tokens = [self._imap[s] for s in self.symbols if s in self._imap]
        tokens.append(NIFTY50_TOKEN)
        return tokens

    @property
    def all_history(self) -> dict[str, pd.DataFrame]:
        """Full history dict {symbol: df} for pre-market filter."""
        return self._history
