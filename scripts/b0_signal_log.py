"""
B0 (step 1) — Signal-firing log generator  (plan_phase1.md §4 B0)

Runs all 37 strategies over raw <= 2018 training data and records every firing
(strategy, direction) per (symbol, day). This is the co-firing dataset the
correlation audit (b0_audit.py) consumes.

Why regenerate: the plan restricts B0 to data <= 2018, but the only trade logs in
the repo are the deprecated fixed-weight system's (2023–2026, and no per-signal
co-firing column). The faithful path is to replay the strategies over 2016–2018.

Output: checkpoints/b0_firings/<SYMBOL>_<YEAR>.parquet  (long format, fires only)
        columns: date, symbol, strategy, direction, signal_time
Resumable: existing per-(symbol,year) files are skipped.

Usage:
    python scripts/b0_signal_log.py --years 2016 2017 2018 --max-stocks 120
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import STOCKS_DIR, CHECKPOINT_DIR          # noqa: E402
from strategies import ALL_STRATEGIES                            # noqa: E402

OUT_DIR = CHECKPOINT_DIR / "b0_firings"
HIST_DAYS = 70          # trailing history window (chart patterns look back ~40-60d)
MIN_TODAY_BARS = 10


def _pick_stocks(years: list[int], max_stocks: int) -> list[str]:
    """Symbols present in every requested year, ranked by mid-year turnover."""
    per_year = []
    for y in years:
        d = STOCKS_DIR / str(y)
        per_year.append({f.stem for f in d.glob("*.parquet")} if d.exists() else set())
    common = set.intersection(*per_year) if per_year else set()
    if not common:
        return []
    rank_year = years[len(years) // 2]
    turnover = {}
    for sym in common:
        f = STOCKS_DIR / str(rank_year) / f"{sym}.parquet"
        try:
            df = pd.read_parquet(f, columns=["close", "volume"])
            turnover[sym] = float((df["close"] * df["volume"]).sum())
        except Exception:
            turnover[sym] = 0.0
    ranked = sorted(common, key=lambda s: turnover.get(s, 0.0), reverse=True)
    return ranked[:max_stocks] if max_stocks else ranked


def _load_stock(sym: str, year: int) -> pd.DataFrame | None:
    """Load year + prev-year (warmup) for one symbol."""
    frames = []
    for y in (year - 1, year):
        f = STOCKS_DIR / str(y) / f"{sym}.parquet"
        if f.exists():
            df = pd.read_parquet(f)
            df["datetime"] = pd.to_datetime(df["datetime"])
            frames.append(df)
    if not frames:
        return None
    out = pd.concat(frames).drop_duplicates("datetime").sort_values("datetime")
    return out.reset_index(drop=True)


def process_stock_year(sym: str, year: int) -> pd.DataFrame:
    df = _load_stock(sym, year)
    if df is None or df.empty:
        return pd.DataFrame()

    df["d"] = df["datetime"].dt.date
    by_date = {d: g.reset_index(drop=True) for d, g in df.groupby("d")}
    all_dates = sorted(by_date)
    target_dates = [d for d in all_dates if d.year == year]

    # daily OHLC per date (for prev_day)
    daily = (df.groupby("d")
               .agg(open=("open", "first"), high=("high", "max"),
                    low=("low", "min"), close=("close", "last"),
                    volume=("volume", "sum")))

    rows = []
    for d in target_dates:
        today = by_date[d]
        if len(today) < MIN_TODAY_BARS:
            continue
        idx = all_dates.index(d)
        hist_dates = all_dates[max(0, idx - HIST_DAYS):idx]
        if not hist_dates:
            continue
        history = pd.concat([by_date[hd] for hd in hist_dates], ignore_index=True)
        prev = daily.loc[hist_dates[-1]]
        for s in ALL_STRATEGIES:
            try:
                sig = s.generate_signal(today_5min=today, history_5min=history,
                                        prev_day=prev, nifty_today=today, trade_date=d)
            except Exception:
                continue
            if sig is not None and sig.direction != 0:
                rows.append((str(d), sym, s.name, int(sig.direction), sig.signal_time))
    return pd.DataFrame(rows, columns=["date", "symbol", "strategy", "direction", "signal_time"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2016, 2017, 2018])
    ap.add_argument("--max-stocks", type=int, default=120)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stocks = _pick_stocks(args.years, args.max_stocks)
    total = len(stocks) * len(args.years)
    print(f"[B0] {len(stocks)} stocks x {len(args.years)} years = {total} stock-years "
          f"-> {OUT_DIR}", flush=True)

    done = 0
    t0 = time.time()
    for sym in stocks:
        for year in args.years:
            out_f = OUT_DIR / f"{sym}_{year}.parquet"
            done += 1
            if out_f.exists():
                continue
            t1 = time.time()
            fires = process_stock_year(sym, year)
            fires.to_parquet(out_f, index=False)
            el = time.time() - t1
            print(f"[B0] {done}/{total} {sym} {year}: {len(fires)} fires ({el:.1f}s) "
                  f"[elapsed {(time.time()-t0)/60:.1f}m]", flush=True)

    print(f"[B0] DONE {done} stock-years in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
