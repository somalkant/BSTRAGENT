"""
Fetches the Nifty 500 constituent symbol list using multiple methods, in order:
  1. NSE Archives direct CSV (static file, no auth needed — most reliable)
  2. NSE API with curl_cffi browser impersonation
  3. NSE API with requests + session cookies
  4. Local cache file (config/nifty500_symbols.csv) if previously saved
"""

from __future__ import annotations

import io
import time
import logging
from pathlib import Path

import requests
import pandas as pd

log = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent / "config" / "nifty500_symbols.csv"

NSE_ARCHIVE_CSV   = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_API_URL       = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500"
NSE_HOME          = "https://www.nseindia.com"
NSE_MARKET_PAGE   = "https://www.nseindia.com/market-data/live-equity-market?symbol=NIFTY%20500"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}


def fetch_nifty500_symbols() -> list[str]:
    """
    Returns list of Nifty 500 trading symbols (e.g. ['RELIANCE', 'HDFCBANK', ...]).
    Tries multiple sources, saves result to cache file on success.
    """
    # Method 1: NSE Archives direct CSV (no cookies, no auth)
    symbols = _try_nse_archive_csv()
    if symbols:
        log.info(f"Method 1 (NSE Archive CSV): fetched {len(symbols)} symbols")
        _save_cache(symbols)
        return symbols

    # Method 2: curl_cffi browser impersonation (if installed)
    symbols = _try_curl_cffi()
    if symbols:
        log.info(f"Method 2 (curl_cffi): fetched {len(symbols)} symbols")
        _save_cache(symbols)
        return symbols

    # Method 3: requests with session + cookie harvest
    symbols = _try_requests_session()
    if symbols:
        log.info(f"Method 3 (requests session): fetched {len(symbols)} symbols")
        _save_cache(symbols)
        return symbols

    # Method 4: local cache from a previous successful run
    symbols = _try_local_cache()
    if symbols:
        log.warning(f"Using cached symbol list ({len(symbols)} symbols) — may be slightly outdated")
        return symbols

    raise RuntimeError(
        "All methods to fetch Nifty 500 symbols failed.\n"
        "Manual fix: download https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv\n"
        "and save it as config/nifty500_symbols.csv with a column named 'Symbol'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Method implementations
# ─────────────────────────────────────────────────────────────────────────────

def _try_nse_archive_csv() -> list[str]:
    try:
        resp = requests.get(NSE_ARCHIVE_CSV, headers=HEADERS, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 1000:
            df = pd.read_csv(io.StringIO(resp.text))
            col = _find_symbol_col(df)
            if col:
                return df[col].dropna().str.strip().tolist()
    except Exception as e:
        log.debug(f"NSE Archive CSV failed: {e}")
    return []


def _try_curl_cffi() -> list[str]:
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome120")
        # Warm up with homepage
        session.get(NSE_HOME, timeout=10)
        time.sleep(1.5)
        resp = session.get(NSE_API_URL, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return [item["symbol"] for item in data.get("data", []) if item.get("symbol")]
    except Exception as e:
        log.debug(f"curl_cffi failed: {e}")
    return []


def _try_requests_session() -> list[str]:
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        # Visit homepage to collect cookies
        session.get(NSE_HOME, timeout=10)
        time.sleep(1.5)
        # Visit market page to get more cookies
        session.get(NSE_MARKET_PAGE, timeout=10)
        time.sleep(1.0)
        # Now hit the API
        resp = session.get(
            NSE_API_URL,
            headers={**HEADERS, "Referer": NSE_HOME, "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200 and resp.text.strip():
            data = resp.json()
            return [item["symbol"] for item in data.get("data", []) if item.get("symbol")]
    except Exception as e:
        log.debug(f"requests session failed: {e}")
    return []


def _try_local_cache() -> list[str]:
    try:
        if CACHE_FILE.exists():
            df = pd.read_csv(CACHE_FILE)
            col = _find_symbol_col(df)
            if col:
                return df[col].dropna().str.strip().tolist()
    except Exception as e:
        log.debug(f"Local cache failed: {e}")
    return []


def _find_symbol_col(df: pd.DataFrame) -> str | None:
    """Find the column containing stock symbols (handles different CSV formats)."""
    for candidate in ["Symbol", "symbol", "SYMBOL", "tradingsymbol", "ticker"]:
        if candidate in df.columns:
            return candidate
    return None


def _save_cache(symbols: list[str]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"Symbol": symbols}).to_csv(CACHE_FILE, index=False)
    log.info(f"Saved {len(symbols)} symbols to {CACHE_FILE}")
