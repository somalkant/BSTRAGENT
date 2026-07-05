#!/usr/bin/env python
"""
Bayesian trading system — full-pipeline runner (Phases 1-3).

This is THE entry point for the Bayesian system (the old run_testing.py / run_analysis.py
drive the deprecated fixed-weight engine and are NOT used here).

Logs stream to the console AND to logs/bayesian_<timestamp>.log so you can watch every
TRADE line, gate decision, regime shift, and skip reason as it runs.

Examples
--------
  # fast sanity run — ~20 liquid stocks, first 10 trading days of 2016 (a couple of minutes)
  python run_bayesian.py smoke

  # train one year (bounded universe so you can watch it) then a bigger one
  python run_bayesian.py train --years 2016 --max-stocks 50 --days 40
  python run_bayesian.py train --years 2016 2017 2018        # full universe, full years (hours)

  # full walk-forward (train->freeze->test across 2016-2026); bound it first to trial the logs
  python run_bayesian.py wf --max-stocks 40 --days 30
  python run_bayesian.py wf                                   # the real 19-step run (long)

Every run ends with a calibration summary (ECE / Brier / EV-realisation / regimes / P&L).
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# Anaconda (or any other Python) commonly sits ahead of a project venv on PATH, so a
# fresh terminal that hasn't run .venv\Scripts\Activate.ps1 will silently launch the
# wrong interpreter — no crash, just a run in the wrong environment writing conflicting
# checkpoints. Fail fast and loud instead.
_venv_dir = "Scripts" if sys.platform == "win32" else "bin"
_venv_exe = "python.exe" if sys.platform == "win32" else "python"
_expected_python = BASE / ".venv" / _venv_dir / _venv_exe
if _expected_python.exists() and Path(sys.executable).resolve() != _expected_python.resolve():
    sys.exit(
        f"Wrong Python interpreter: {sys.executable}\n"
        f"This project's venv is at: {_expected_python}\n"
        f"Run instead:\n"
        f'  "{_expected_python}" {" ".join(sys.argv)}'
    )

from config.settings import PAPER_TRADES_FILE, CHECKPOINT_DIR   # noqa: E402

# ── prevent Windows sleep (no-op on Linux/EC2) — same pattern as TradingAgent's
# run_live.py. Tied to this process's thread: Windows clears the flag automatically
# when the process exits, even on a crash or a forceful kill, so this can't leave the
# machine permanently unable to sleep.
ES_CONTINUOUS      = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _keep_awake() -> None:
    if platform.system() == "Windows":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def _allow_sleep() -> None:
    if platform.system() == "Windows":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def setup_logging(level: str) -> Path:
    logs_dir = BASE / "logs"
    logs_dir.mkdir(exist_ok=True)
    logfile = logs_dir / f"bayesian_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = "%(asctime)s %(levelname)-5s | %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt, datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(logfile, encoding="utf-8")],
    )
    # quiet noisy third-party libs
    for noisy in ("matplotlib", "numexpr"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logfile


def calibration_summary(paper_file: Path) -> None:
    import pandas as pd
    from reports import calibration as cal
    log = logging.getLogger("summary")
    if not paper_file.exists():
        log.warning("no trades were produced (%s absent) — nothing to summarise", paper_file.name)
        return
    df = pd.read_csv(paper_file, on_bad_lines="skip")
    if df.empty:
        log.warning("no trades produced.")
        return
    rep = cal.generate_report(df)
    wins = int((df["pnl_rs"] > 0).sum())
    log.info("=" * 70)
    log.info("PIPELINE SUMMARY")
    log.info("  trades: %d | wins: %d (%.1f%%) | net PnL: Rs %s",
             len(df), wins, wins / len(df) * 100, f"{df['pnl_rs'].sum():,.0f}")
    if "regime" in df.columns:
        log.info("  regimes: %s", dict(df["regime"].value_counts()))
    if "driver_strategy" in df.columns:
        top = df["driver_strategy"].value_counts().head(8).to_dict()
        log.info("  top drivers: %s", top)
    log.info("  ECE: %s | Brier: %s | EV-realisation ratio: %s | drift alarms: %s",
             rep["ece"]["ece"], rep["brier"]["brier"],
             rep["ev_realisation"]["ratio"], rep["prob_drift_alarms"])
    log.info("  loss-decomposition: %s", rep["loss_decomposition"])
    log.info("  max trade risk: %.4f%% (cap 0.5%%)",
             df["risk_pct"].max() if "risk_pct" in df.columns else float("nan"))
    log.info("=" * 70)


def _fresh(paper_file: Path) -> None:
    if paper_file.exists():
        paper_file.unlink()


def main():
    ap = argparse.ArgumentParser(description="Bayesian trading system pipeline")
    ap.add_argument("mode", choices=["smoke", "train", "wf"], help="what to run")
    ap.add_argument("--years", type=int, nargs="+", default=None, help="train mode: years")
    ap.add_argument("--max-stocks", type=int, default=None, help="limit universe (fast runs)")
    ap.add_argument("--days", type=int, default=None, help="limit trading days per year")
    ap.add_argument("--windows", type=int, nargs="+", default=None, help="wf mode: subset of WF ids")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    ap.add_argument("--keep", action="store_true", help="append to the existing paper log (default: fresh)")
    args = ap.parse_args()

    logfile = setup_logging(args.log_level)
    log = logging.getLogger("run")
    log.info("Bayesian pipeline — mode=%s | log file: %s", args.mode, logfile)
    t0 = time.time()

    _keep_awake()
    log.info("Windows sleep prevention: ON")
    try:
        # import after logging is configured so their module-level [PIT_MISSING] notes are captured
        from backtester import bayesian_engine as be
        from scripts.walk_forward import run_walk_forward

        if args.mode == "smoke":
            if not args.keep:
                _fresh(PAPER_TRADES_FILE)
            max_stocks = args.max_stocks or 20
            days = args.days or 10
            log.info("SMOKE: %d stocks x %d days of 2016 (proves the full engine + all layers)", max_stocks, days)
            s = be.run_year_bayesian(2016, save_state=False, max_stocks=max_stocks, days_limit=days)
            log.info("smoke year summary: %s", s)
            calibration_summary(PAPER_TRADES_FILE)

        elif args.mode == "train":
            years = args.years or [2016, 2017, 2018]
            if not args.keep:
                _fresh(PAPER_TRADES_FILE)
            from weights.bayesian import BayesianState
            from weights.changepoint import ChangePointMonitor
            from weights.stock_type import StockTypePrior
            from weights.regime import RegimeClassifier
            bayes = BayesianState(); bayes.attach_changepoint(ChangePointMonitor())
            stock_type = StockTypePrior(); classifier = RegimeClassifier()
            for yr in years:
                log.info("---- TRAIN %d ----", yr)
                s = be.run_year_bayesian(yr, bayes=bayes, stock_type=stock_type, classifier=classifier,
                                         save_state=True, max_stocks=args.max_stocks, days_limit=args.days)
                log.info("year %d summary: %s", yr, s)
            calibration_summary(PAPER_TRADES_FILE)

        elif args.mode == "wf":
            log.info("WALK-FORWARD: %s windows | max_stocks=%s days=%s",
                     args.windows or "all 8", args.max_stocks, args.days)
            windows = None
            if args.windows:
                from config.settings import WF_WINDOWS
                windows = [w for w in WF_WINDOWS if w["wf"] in args.windows]
            results = run_walk_forward(days_limit=args.days, windows=windows, max_stocks=args.max_stocks)
            for r in results:
                log.info("WF step: %s", r)
            # summarise the most recent test-year paper log
            test_logs = sorted(CHECKPOINT_DIR.glob("wf*_test_*.csv"))
            if test_logs:
                calibration_summary(test_logs[-1])

        log.info("DONE in %.1f min. Full log: %s", (time.time() - t0) / 60, logfile)
    finally:
        _allow_sleep()
        log.info("Windows sleep prevention: OFF")


if __name__ == "__main__":
    main()
