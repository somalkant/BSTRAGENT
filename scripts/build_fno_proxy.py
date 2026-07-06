"""
Liquidity-proxy builder for config/fno_membership.json (plan_phase1.md PIT F&O gap).

The real point-in-time F&O membership has to be reconstructed from NSE circulars —
neither Kite Connect nor any NSE endpoint serves historical membership (both only
expose "today's" list). This script is a documented APPROXIMATION, not that
reconstruction: F&O eligibility is liquidity/market-cap driven (SEBI criteria), so
each period's eligible set is proxied as the top-N stocks by trailing average daily
traded value (ADV = mean of sum(close*volume) per day), ranked using only data
available BEFORE the period starts (no lookahead).

Period granularity is half-year ("YYYY-H1"/"YYYY-H2") so the one big documented
discontinuity in the real list — NSE's Nov-29-2024 addition of 45 stocks — lands in
its own bucket instead of being smeared across 2024. backtester/universe.py's
fno_eligible_short() falls back from exact-date -> half-year -> year -> "_latest".

Target N per period is anchored to researched real F&O counts (approximate, not
exact — this is a proxy):
  2016-2017: 175   (~173 reported May 2016)
  2018-2021: 180
  2022-2023: 190   (~175 Nov 2022, ~191 Feb 2023)
  2024-H1  : 190   (pre Nov-29 addition)
  2024-H2+ : 230   (post the +45 addition, ~235 raw)
  2026     : 214   (reported current count as of this build)

Replace this file's output with a real NSE-circular-sourced fno_membership.json
before the actual B6 walk-forward run counts for anything (plan_phase1.md B2 bar:
"fno_membership.json date-keyed and sourced from NSE circulars").

Usage: python scripts/build_fno_proxy.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import STOCKS_DIR, FNO_MEMBERSHIP_FILE   # noqa: E402
from backtester.universe import universe_for_year              # noqa: E402

# period -> (source_year_for_ranking, target_N)
# source_year is the trailing/prior year whose data ranks eligibility (no lookahead);
# 2016 has no prior year in the dataset, so it bootstraps off its own first 60 days.
PERIOD_PLAN: list[tuple[str, int, int]] = [
    ("2016-H1", 2016, 175),   # bootstrap: no 2015 data available
    ("2016-H2", 2016, 175),   # bootstrap: no 2015 data available
    ("2017-H1", 2016, 175),
    ("2017-H2", 2016, 175),
    ("2018-H1", 2017, 180),
    ("2018-H2", 2017, 180),
    ("2019-H1", 2018, 180),
    ("2019-H2", 2018, 180),
    ("2020-H1", 2019, 180),
    ("2020-H2", 2019, 180),
    ("2021-H1", 2020, 180),
    ("2021-H2", 2020, 180),
    ("2022-H1", 2021, 190),
    ("2022-H2", 2021, 190),
    ("2023-H1", 2022, 190),
    ("2023-H2", 2022, 190),
    ("2024-H1", 2023, 190),
    ("2024-H2", 2023, 230),   # Nov 29 2024 addition lands here
    ("2025-H1", 2024, 230),
    ("2025-H2", 2024, 230),
    ("2026-H1", 2025, 214),
]

BOOTSTRAP_DAYS = 60   # 2016-only: first N trading days used for its own ranking


def _daily_traded_value(year: int, symbols: set[str], bootstrap_days: int | None = None) -> pd.Series:
    """mean per-symbol daily traded value (sum(close*volume) per calendar day)."""
    adv = {}
    for sym in symbols:
        f = STOCKS_DIR / str(year) / f"{sym}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f, columns=["datetime", "close", "volume"])
        if bootstrap_days is not None:
            first_days = pd.Index(df["datetime"].dt.date.unique()).sort_values()[:bootstrap_days]
            df = df[df["datetime"].dt.date.isin(first_days)]
        if df.empty:
            continue
        daily_value = (df["close"] * df["volume"]).groupby(df["datetime"].dt.date).sum()
        adv[sym] = daily_value.mean()
    return pd.Series(adv)


def build() -> dict:
    out: dict[str, list[str] | str] = {}
    ranked_cache: dict[int, pd.Series] = {}
    for period, source_year, n in PERIOD_PLAN:
        universe = universe_for_year(source_year) or universe_for_year(int(period[:4]))
        bootstrap = BOOTSTRAP_DAYS if period.startswith("2016") else None
        cache_key = (source_year, bootstrap)
        if cache_key not in ranked_cache:
            ranked_cache[cache_key] = _daily_traded_value(source_year, universe, bootstrap)
        adv = ranked_cache[cache_key]
        top_n = adv.sort_values(ascending=False).head(n).index.tolist()
        out[period] = sorted(top_n)
        print(f"{period}: ranked on {source_year} data"
              f"{' (bootstrap first %d days)' % BOOTSTRAP_DAYS if bootstrap else ''}, "
              f"{len(top_n)} symbols")
    out["_latest"] = out[PERIOD_PLAN[-1][0]]
    return out


if __name__ == "__main__":
    membership = build()
    FNO_MEMBERSHIP_FILE.write_text(json.dumps(membership, indent=2))
    print(f"\nWrote {FNO_MEMBERSHIP_FILE} ({FNO_MEMBERSHIP_FILE.stat().st_size / 1024:.0f} KB)")
