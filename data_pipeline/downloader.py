"""
Year-by-year historical data downloader for 5-minute OHLCV candles.

Usage:
    from data_pipeline.downloader import download_year
    download_year(kite, year=2016)

Features:
  - Downloads 1 full calendar year for all 500 stocks
  - Chunks requests into 95-day windows (safe below Kite's 100-day limit)
  - Rate-limits to 3 req/sec (sleeps 0.38s between calls)
  - Tracks per-stock progress in checkpoints/progress.json
  - Resumes automatically if interrupted — skips already-completed stocks
  - Retries failed stocks up to 3 times with exponential backoff
  - Saves each stock as data/stocks/SYMBOL.parquet (appends new years)
  - Also downloads NIFTY50 index and INDIA VIX
"""

from __future__ import annotations

import json
import time
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

if TYPE_CHECKING:
    from kiteconnect import KiteConnect   # type hints only — not imported at runtime

from config.settings import (
    DATA_INTERVAL, CHUNK_DAYS, RATE_LIMIT_SLEEP,
    MAX_RETRIES, RETRY_BACKOFF,
    STOCKS_DIR, INDEX_DIR, PROGRESS_FILE, UNIVERSE_FILE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def download_eod(
    kite: "KiteConnect",
    today: date,
    imap: dict | None = None,
) -> dict:
    """
    Download today's complete 5-min OHLCV bars for all 500 universe stocks.

    Called automatically by the live agent after market close (15:15–15:30).
    Appends today's bars to each stock's year parquet file so tomorrow's
    agent startup reads a fully up-to-date history without any manual download.

    Args:
        kite:  KiteConnect or GrowwClientAdapter (both support historical_data())
        today: Date to download data for
        imap:  {symbol: instrument_token} from load_instrument_map(). If provided,
               broker-specific tokens are used (Groww mode). If None, Zerodha tokens
               are loaded from universe.csv (default Zerodha behaviour).

    Returns:
        {"completed": int, "failed": int, "failed_symbols": list[str]}
    """
    universe  = _load_universe()
    year_str  = str(today.year)

    stocks_dir = STOCKS_DIR / year_str
    index_dir  = INDEX_DIR  / year_str
    stocks_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)

    from_date = today
    to_date   = today

    total     = len(universe)
    completed = 0
    failed    = []

    log.info(f"EOD download — {today} — {total} stocks"
             + (" [Groww tokens]" if imap is not None else " [Zerodha tokens]"))

    for i, row in enumerate(universe.itertuples(index=False), start=1):
        symbol = row.tradingsymbol

        if imap is not None:
            token = imap.get(symbol)
            if token is None:
                log.debug(f"  {symbol}: not in broker instrument map — skipping")
                continue
            token = int(token)
        else:
            token = int(row.instrument_token)

        if i % 50 == 0 or i == total:
            log.info(f"  EOD progress: {i}/{total} stocks processed ({completed} ok, {len(failed)} failed)")

        ok = _download_stock(kite, symbol, token, from_date, to_date, stocks_dir)
        if ok:
            completed += 1
        else:
            failed.append(symbol)

    # Download NIFTY50 — works for both Zerodha and Groww (special-cased in GrowwClientAdapter)
    log.info("  EOD: downloading NIFTY50 index...")
    _download_index(kite, "NIFTY 50", 256265, from_date, to_date, "NIFTY50", index_dir)

    # INDIA VIX — Zerodha only; Groww returns empty silently (no error)
    log.info("  EOD: downloading INDIA VIX...")
    _download_index(kite, "INDIA VIX", 264969, from_date, to_date, "INDIAVIX", index_dir)

    if failed:
        log.warning(f"  EOD: {len(failed)} stocks failed — {failed[:10]}{'...' if len(failed) > 10 else ''}")
    log.info(f"EOD download complete — {completed}/{total} stocks saved for {today}")

    return {"completed": completed, "failed": len(failed), "failed_symbols": failed}


def fill_gaps(
    client: "KiteConnect",
    imap: dict,
    today: date,
) -> dict:
    """
    Detect and fill missing trading days in parquet files at agent startup.

    Checks each universe stock's parquet for the latest date. If the last date
    is before the most recent trading day (skipping weekends), downloads the
    missing days using the active broker's historical_data() API.

    This makes the live agent self-healing: if yesterday's EOD download was
    missed (e.g. session ended before 15:31, or ran on Zerodha while Groww
    was active), today's startup fills the gap automatically.

    Args:
        client: KiteConnect or GrowwClientAdapter
        imap:   {symbol: instrument_token} — broker-specific tokens from load_instrument_map()
        today:  Today's date (gap is filled up to the last trading day before today)

    Returns:
        {"filled": int, "skipped": int, "failed": list[str]}
    """
    last_td = _last_trading_day(today)

    # Holiday probe: confirm last_td was an actual trading day using one liquid stock.
    # If the probe returns no candles, step back until we find a real trading day.
    # Prevents wasting 500 API calls when the prior weekday was a market holiday.
    probe_token = imap.get("RELIANCE") or imap.get("INFY") or imap.get("TCS")
    if probe_token:
        max_probe = 10  # never step back more than 10 calendar days
        for _ in range(max_probe):
            try:
                time.sleep(RATE_LIMIT_SLEEP)
                probe_data = client.historical_data(
                    instrument_token=int(probe_token),
                    from_date=last_td,
                    to_date=last_td,
                    interval=DATA_INTERVAL,
                )
                if probe_data:
                    break  # last_td is a real trading day
            except Exception:
                pass
            log.info(f"Gap-fill probe: {last_td} returned no data — NSE holiday, stepping back")
            last_td = _last_trading_day(last_td)  # try the day before

    try:
        universe = _load_universe()
    except FileNotFoundError:
        log.warning("fill_gaps: universe.csv not found — skipping gap check")
        return {"filled": 0, "skipped": 0, "failed": []}

    filled   = 0
    skipped  = 0
    failed   = []

    symbols_needing_fill = []
    for row in universe.itertuples(index=False):
        symbol     = row.tradingsymbol
        last_date  = _get_last_parquet_date(symbol, today)

        if last_date is None:
            skipped += 1
            continue  # no parquet yet — not our job (use download_year)

        if last_date >= last_td:
            skipped += 1
            continue  # up to date

        token = imap.get(symbol)
        if token is None:
            log.debug(f"  gap-fill: {symbol} not in broker imap — skipping")
            skipped += 1
            continue

        symbols_needing_fill.append((symbol, int(token), last_date))

    if not symbols_needing_fill:
        log.info(f"Gap-fill: all parquet files are up to date (last trading day: {last_td})")
        return {"filled": 0, "skipped": skipped, "failed": []}

    log.info(f"Gap-fill: {len(symbols_needing_fill)} stocks missing data since before {last_td} — downloading...")

    for symbol, token, last_date in symbols_needing_fill:
        gap_from = last_date + timedelta(days=1)
        gap_to   = last_td
        log.info(f"  Gap-fill: {symbol} — {gap_from} → {gap_to}")

        ok = True
        # Handle year-boundary gaps by splitting per year
        for yr in range(gap_from.year, gap_to.year + 1):
            yr_from     = max(gap_from, date(yr, 1, 1))
            yr_to       = min(gap_to,   date(yr, 12, 31))
            stocks_dir  = STOCKS_DIR / str(yr)
            stocks_dir.mkdir(parents=True, exist_ok=True)
            if not _download_stock(client, symbol, token, yr_from, yr_to, stocks_dir):
                ok = False

        if ok:
            filled += 1
        else:
            failed.append(symbol)

    log.info(f"Gap-fill complete: {filled} filled, {len(failed)} failed"
             + (f" — {failed}" if failed else ""))
    return {"filled": filled, "skipped": skipped, "failed": failed}


def _last_trading_day(today: date) -> date:
    """Return the most recent weekday strictly before today (Mon–Fri only)."""
    delta = 1
    while True:
        candidate = today - timedelta(days=delta)
        if candidate.weekday() < 5:   # 0=Mon … 4=Fri
            return candidate
        delta += 1


def _get_last_parquet_date(symbol: str, today: date) -> date | None:
    """Return the latest date found in the symbol's parquet files, or None."""
    last_date = None
    for yr in [today.year - 1, today.year]:
        path = STOCKS_DIR / str(yr) / f"{symbol}.parquet"
        if not path.exists():
            continue
        try:
            df    = pd.read_parquet(path, columns=["datetime"])
            max_d = pd.to_datetime(df["datetime"]).dt.date.max()
            if last_date is None or max_d > last_date:
                last_date = max_d
        except Exception:
            pass
    return last_date


def download_year(kite: "KiteConnect", year: int, force: bool = False) -> dict:
    """
    Download all 5-min data for a given year for all stocks in universe.

    Args:
        kite:  Authenticated KiteConnect instance (from kite_auth.get_kite())
        year:  Calendar year to download (e.g. 2016)
        force: Re-download even if stocks already marked complete

    Returns:
        Summary dict with counts of completed / failed stocks
    """
    universe = _load_universe()
    progress = _load_progress()
    year_str  = str(year)

    if year_str not in progress["download"]:
        progress["download"][year_str] = {"completed": [], "failed": []}

    already_done = set(progress["download"][year_str]["completed"])
    stocks_to_do = [
        row for _, row in universe.iterrows()
        if force or row["tradingsymbol"] not in already_done
    ]

    from_date = date(year, 1, 1)
    to_date   = date(year, 12, 31)

    log.info(f"Downloading year {year} — {len(stocks_to_do)} stocks to process")
    log.info(f"Date range: {from_date}  →  {to_date}")

    year_stocks_dir = STOCKS_DIR / str(year)
    year_index_dir  = INDEX_DIR  / str(year)
    year_stocks_dir.mkdir(parents=True, exist_ok=True)
    year_index_dir.mkdir(parents=True, exist_ok=True)

    failed = []
    with tqdm(stocks_to_do, desc=f"Year {year}", unit="stock", ncols=80) as pbar:
        for row in pbar:
            symbol = row["tradingsymbol"]
            token  = int(row["instrument_token"])
            pbar.set_postfix({"stock": symbol})

            ok = _download_stock(kite, symbol, token, from_date, to_date, year_stocks_dir)
            if ok:
                progress["download"][year_str]["completed"].append(symbol)
            else:
                progress["download"][year_str]["failed"].append(symbol)
                failed.append(symbol)

            _save_progress(progress)

    # Download index instruments
    log.info("Downloading NIFTY50 index data...")
    _download_index(kite, "NIFTY 50",  256265, from_date, to_date, "NIFTY50",  year_index_dir)
    log.info("Downloading INDIA VIX data...")
    _download_index(kite, "INDIA VIX", 264969, from_date, to_date, "INDIAVIX", year_index_dir)

    progress["download"][year_str]["status"] = "completed" if not failed else "partial"
    _save_progress(progress)

    completed = len(progress["download"][year_str]["completed"])
    log.info(f"Year {year} done: {completed} completed, {len(failed)} failed")
    if failed:
        log.warning(f"Failed stocks: {failed}")

    return {"year": year, "completed": completed, "failed": failed}


def retry_failed(kite: "KiteConnect", year: int) -> dict:
    """Re-attempt all stocks that failed in a previous download_year() call."""
    progress = _load_progress()
    year_str = str(year)
    failed   = progress["download"].get(year_str, {}).get("failed", [])

    if not failed:
        log.info(f"No failed stocks for year {year}.")
        return {"year": year, "retried": 0, "still_failed": []}

    universe = _load_universe()
    universe = universe[universe["tradingsymbol"].isin(failed)]

    from_date = date(year, 1, 1)
    to_date   = date(year, 12, 31)

    still_failed = []
    for _, row in tqdm(universe.iterrows(), total=len(universe), desc="Retrying"):
        symbol = row["tradingsymbol"]
        token  = int(row["instrument_token"])
        ok = _download_stock(kite, symbol, token, from_date, to_date)
        if ok:
            progress["download"][year_str]["completed"].append(symbol)
            progress["download"][year_str]["failed"].remove(symbol)
        else:
            still_failed.append(symbol)
        _save_progress(progress)

    return {"year": year, "retried": len(failed), "still_failed": still_failed}


def get_download_status() -> dict:
    """Show download progress across all years."""
    progress = _load_progress()
    universe = _load_universe()
    total    = len(universe)

    status = {}
    for year_str, data in progress.get("download", {}).items():
        done = len(data.get("completed", []))
        fail = len(data.get("failed", []))
        status[year_str] = {
            "completed": done,
            "failed":    fail,
            "pending":   total - done - fail,
            "total":     total,
            "pct":       f"{100 * done / total:.1f}%",
            "status":    data.get("status", "in_progress"),
        }
    return status


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _download_stock(
    kite: "KiteConnect",
    symbol: str,
    token: int,
    from_date: date,
    to_date: date,
    stocks_dir: Path = STOCKS_DIR,
) -> bool:
    """
    Download all chunks for one stock in one year. Returns True on success.
    Appends to existing Parquet file (adds new year's data without overwriting old years).
    """
    chunks = _make_date_chunks(from_date, to_date, CHUNK_DAYS)
    all_rows = []

    for chunk_from, chunk_to in chunks:
        for attempt in range(MAX_RETRIES):
            try:
                time.sleep(RATE_LIMIT_SLEEP)
                raw = kite.historical_data(
                    instrument_token=token,
                    from_date=chunk_from,
                    to_date=chunk_to,
                    interval=DATA_INTERVAL,
                )
                if raw:
                    all_rows.extend(raw)
                break
            except Exception as e:
                wait = RETRY_BACKOFF[attempt] if attempt < len(RETRY_BACKOFF) else 60
                log.warning(f"{symbol} chunk {chunk_from}→{chunk_to} attempt {attempt+1}: {e}. Retry in {wait}s")
                time.sleep(wait)
        else:
            log.error(f"{symbol}: all retries exhausted for chunk {chunk_from}→{chunk_to}")
            return False

    if not all_rows:
        log.warning(f"{symbol}: no data returned for {from_date}→{to_date}")
        return False

    df = pd.DataFrame(all_rows)
    df.rename(columns={"date": "datetime"}, inplace=True)
    df["symbol"] = symbol
    df = df[["datetime", "symbol", "open", "high", "low", "close", "volume"]]
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype("int64")
    df.sort_values("datetime", inplace=True)
    df.drop_duplicates(subset=["datetime"], inplace=True)

    _append_parquet(stocks_dir / f"{symbol}.parquet", df)
    return True


def _download_index(
    kite: "KiteConnect",
    name: str,
    token: int,
    from_date: date,
    to_date: date,
    filename: str,
    index_dir: Path = INDEX_DIR,
) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    chunks   = _make_date_chunks(from_date, to_date, CHUNK_DAYS)
    all_rows = []

    for chunk_from, chunk_to in chunks:
        try:
            time.sleep(RATE_LIMIT_SLEEP)
            raw = kite.historical_data(token, chunk_from, chunk_to, DATA_INTERVAL)
            all_rows.extend(raw)
        except Exception as e:
            log.warning(f"{name} chunk {chunk_from}→{chunk_to}: {e}")

    if not all_rows:
        return

    df = pd.DataFrame(all_rows)
    df.rename(columns={"date": "datetime"}, inplace=True)
    df["symbol"] = filename
    df = df[["datetime", "symbol", "open", "high", "low", "close", "volume"]]
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype("int64")
    df.sort_values("datetime", inplace=True)
    df.drop_duplicates(subset=["datetime"], inplace=True)
    _append_parquet(index_dir / f"{filename}.parquet", df)


def _append_parquet(path: Path, df: pd.DataFrame) -> None:
    """Append new rows to a Parquet file, or create it if it doesn't exist."""
    table = pa.Table.from_pandas(df, preserve_index=False)
    if path.exists():
        existing = pq.read_table(path)
        # Cast new table to match the existing file's schema exactly.
        # Kite sometimes returns integer prices for whole-number stocks,
        # which causes an int64 vs double mismatch on concat.
        try:
            table = table.cast(existing.schema)
        except Exception:
            # Fallback: unify via pandas (handles edge-case type promotions)
            merged_df = (pd.concat([existing.to_pandas(), df])
                         .drop_duplicates(subset=["datetime"])
                         .sort_values("datetime")
                         .reset_index(drop=True))
            for col in ["open", "high", "low", "close"]:
                merged_df[col] = merged_df[col].astype(float)
            merged_df["volume"] = merged_df["volume"].astype("int64")
            pq.write_table(pa.Table.from_pandas(merged_df, preserve_index=False), path, compression="snappy")
            return
        merged   = pa.concat_tables([existing, table])
        merged_df = merged.to_pandas().drop_duplicates(subset=["datetime"]).sort_values("datetime")
        pq.write_table(pa.Table.from_pandas(merged_df, preserve_index=False), path, compression="snappy")
    else:
        pq.write_table(table, path, compression="snappy")


def _make_date_chunks(from_date: date, to_date: date, chunk_days: int) -> list[tuple]:
    """Split a date range into chunks of max chunk_days each."""
    chunks = []
    current = from_date
    while current <= to_date:
        end = min(current + timedelta(days=chunk_days - 1), to_date)
        chunks.append((current, end))
        current = end + timedelta(days=1)
    return chunks


def _load_universe() -> pd.DataFrame:
    if not UNIVERSE_FILE.exists():
        raise FileNotFoundError(
            f"Universe file not found: {UNIVERSE_FILE}\n"
            "Run the 'Build Universe' section of the setup notebook first."
        )
    return pd.read_csv(UNIVERSE_FILE)


def _load_progress() -> dict:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"download": {}, "analysis": {}}


def _save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2, default=str)
