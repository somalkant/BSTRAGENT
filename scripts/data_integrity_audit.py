"""
B2 — Data-integrity audit (plan_phase1.md "Data Integrity — Corporate Action")

Scans the stock store for overnight gaps > 25% and mis-scaled ("glitch") stock-days,
and writes reports/data_integrity.md. Prices are already CA-adjusted, so surviving
gaps are mostly data glitches (e.g. PIIND 2018) or genuine news events.

Usage:
    python scripts/data_integrity_audit.py --years 2016 2017 2018
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import STOCKS_DIR, REPORTS_DIR         # noqa: E402
from backtester.universe import GAP_AUDIT_THRESHOLD, GLITCH_MEDIAN_DEVIATION  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2016, 2017, 2018])
    args = ap.parse_args()

    gaps, glitches, scanned = [], [], 0
    for y in args.years:
        d = STOCKS_DIR / str(y)
        if not d.exists():
            continue
        for f in sorted(d.glob("*.parquet")):
            scanned += 1
            df = pd.read_parquet(f, columns=["datetime", "open", "high", "low", "close"])
            df["datetime"] = pd.to_datetime(df["datetime"])
            daily = df.groupby(df["datetime"].dt.date).agg(
                op=("open", "first"), cl=("close", "last"),
                med=("close", "median"))
            prev_cl = daily["cl"].shift(1)
            gap = (daily["op"] - prev_cl).abs() / prev_cl
            for dt, g in gap[gap > GAP_AUDIT_THRESHOLD].dropna().items():
                gaps.append((f.stem, y, str(dt), round(float(g) * 100, 1)))
            trail = daily["med"].rolling(5).median().shift(1)
            dev = (daily["med"] - trail).abs() / trail
            for dt, dv in dev[dev > GLITCH_MEDIAN_DEVIATION].dropna().items():
                glitches.append((f.stem, y, str(dt), round(float(dv) * 100, 1)))

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    gaps.sort(key=lambda x: -x[3])
    glitches.sort(key=lambda x: -x[3])
    lines = [
        "# Data-Integrity Audit", "",
        f"- scanned **{scanned}** stock-years ({args.years})",
        f"- overnight gaps > {GAP_AUDIT_THRESHOLD:.0%}: **{len(gaps)}**",
        f"- mis-scale glitch days (> {GLITCH_MEDIAN_DEVIATION:.0%} off trailing median, "
        f"excluded at runtime): **{len(glitches)}**", "",
        "## Overnight gaps > 25% (verify vs CA/news; glitches are excluded at runtime)", "",
    ]
    lines += [f"- {s} {y} {dt}: {g}%" for s, y, dt, g in gaps[:60]] or ["- none"]
    lines += ["", "## Excluded glitch stock-days", ""]
    lines += [f"- {s} {y} {dt}: {g}% off trailing median" for s, y, dt, g in glitches[:60]] or ["- none"]
    (REPORTS_DIR / "data_integrity.md").write_text("\n".join(lines))
    print(f"[INTEGRITY] {scanned} stock-years: {len(gaps)} gaps>25%, {len(glitches)} glitch days "
          f"-> reports/data_integrity.md")


if __name__ == "__main__":
    main()
