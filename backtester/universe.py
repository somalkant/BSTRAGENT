"""
B2 — Point-in-time universe, eligibility & data-integrity (plan_phase1.md B2)

Reality of the local dataset (verified):
  * Per-year stock folders already grow 334(2016)->500(2025), so "has a parquet for
    year Y" is a valid point-in-time Nifty-500 universe proxy — the engine's
    per-year data load already realises this. No external membership file needed.
  * Prices are already split/bonus ADJUSTED (only ~75 gaps >25% across 1,052
    stock-years, mostly data glitches like PIIND 2018 mis-scaled bars). So corporate
    actions reduce to a DATA-INTEGRITY gate here, not an external CA calendar.

Graceful degradation: F&O / sector / ASM-GSM point-in-time files are OPTIONAL. When
absent the loader logs [PIT_MISSING] once and falls back to a Phase-1 approximation
(shorts allowed universe-wide; sector rule skipped). Tighten with real NSE history
before the Phase 3 WF run.
"""
from __future__ import annotations

import json
import logging

from config.settings import (
    STOCKS_DIR, FNO_MEMBERSHIP_FILE, SECTOR_MAP_FILE, ASM_GSM_HISTORY_FILE,
)

log = logging.getLogger(__name__)

GAP_AUDIT_THRESHOLD    = 0.25     # log overnight gaps beyond this (plan audit gate)
GLITCH_MEDIAN_DEVIATION = 0.40    # exclude a day whose median price deviates this far
                                  # from the trailing median (mis-scaled bars = bad data)
_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


# ── point-in-time universe (folder-presence proxy) ───────────────────────────
def universe_for_year(year: int) -> set[str]:
    d = STOCKS_DIR / str(year)
    return {f.stem for f in d.glob("*.parquet")} if d.exists() else set()


def in_universe(symbol: str, year: int) -> bool:
    return (STOCKS_DIR / str(year) / f"{symbol}.parquet").exists()


# ── data-integrity gate (corporate-action / glitch guard) ────────────────────
def overnight_gap(today_open: float, prev_close: float) -> float:
    if not prev_close or prev_close <= 0:
        return 0.0
    return abs(today_open - prev_close) / prev_close


def data_integrity_ok(today_median_price: float, trailing_median_price: float,
                      symbol: str = "", trade_date=None) -> bool:
    """
    Exclude a stock-day whose price scale is clearly corrupt (e.g. PIIND 2018 bars at
    ~1/10th of the real level). A legitimate 25-40% news gap is NOT excluded here; only
    gross mis-scales (>40% off the trailing median) are dropped as bad data.
    """
    if not trailing_median_price or trailing_median_price <= 0:
        return True
    dev = abs(today_median_price - trailing_median_price) / trailing_median_price
    if dev > GLITCH_MEDIAN_DEVIATION:
        log.info(f"[DATA_INTEGRITY_SKIP {symbol} {trade_date} dev={dev*100:.0f}% "
                 f"today={today_median_price:.1f} trail={trailing_median_price:.1f}]")
        return False
    return True


# ── eligibility (optional PIT files; graceful fallback) ──────────────────────
def _load_optional(path, key: str):
    if not path.exists():
        _warn_once(key, f"[PIT_MISSING] {path.name} absent — Phase-1 approximation in use")
        return None
    return json.loads(path.read_text())


_FNO     = _load_optional(FNO_MEMBERSHIP_FILE, "fno")
_SECTOR  = _load_optional(SECTOR_MAP_FILE, "sector")
_ASM_GSM = _load_optional(ASM_GSM_HISTORY_FILE, "asm_gsm")


def _half_year_key(trade_date) -> str:
    s = str(trade_date)
    month = int(s[5:7])
    return f"{s[:4]}-H{1 if month <= 6 else 2}"


def fno_eligible_short(symbol: str, trade_date) -> bool:
    """
    SHORT candidates are F&O-only. Without the PIT F&O file we approximate by allowing
    the short (Phase-1), flagged. With the file, membership is queried as-of the date,
    falling back from exact-date keys to half-year/year buckets (the liquidity-proxy
    builder in scripts/build_fno_proxy.py writes half-year granularity, not daily).
    """
    if _FNO is None:
        return True
    key = str(trade_date)
    members = (_FNO.get(key) or _FNO.get(_half_year_key(trade_date))
               or _FNO.get(key[:4]) or _FNO.get("_latest") or [])
    return symbol in members


def long_eligible(symbol: str, trade_date) -> bool:
    """LONG excludes T2T/BE, GSM>=2, ASM long-term (compulsory-delivery) as-of the date."""
    if _ASM_GSM is None:
        return True
    flags = (_ASM_GSM.get(str(trade_date), {}) or {}).get(symbol)
    if not flags:
        return True
    return not (flags.get("t2t") or flags.get("be") or
                (flags.get("gsm_stage", 0) or 0) >= 2 or flags.get("asm_longterm"))


def sector_of(symbol: str, trade_date=None) -> str | None:
    """NSE sector as-of the date (or current). None when the map is absent -> rule skipped."""
    if _SECTOR is None:
        return None
    if isinstance(_SECTOR, dict) and "by_symbol" in _SECTOR:
        return _SECTOR["by_symbol"].get(symbol)
    return _SECTOR.get(symbol) if isinstance(_SECTOR, dict) else None
