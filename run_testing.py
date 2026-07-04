"""
Phase 2 / Phase 2B Testing Runner — frozen weights, 1 trade/day, strict forward-only.

Usage:
    .\\venv\\Scripts\\python.exe run_testing.py 2023
    .\\venv\\Scripts\\python.exe run_testing.py 2024
    .\\venv\\Scripts\\python.exe run_testing.py all        # 2023 through 2026
    .\\venv\\Scripts\\python.exe run_testing.py 2023 --no-pre-filter

Walk-Forward testing (Phase 2B) — use frozen WF weights instead of live weights:
    .\\venv\\Scripts\\python.exe run_testing.py 2019 --wf-window 1
    .\\venv\\Scripts\\python.exe run_testing.py 2020 --wf-window 2
    .\\venv\\Scripts\\python.exe run_testing.py 2021 --wf-window 3
    .\\venv\\Scripts\\python.exe run_testing.py 2022 --wf-window 4
    .\\venv\\Scripts\\python.exe run_testing.py 2023 --wf-window 5
    .\\venv\\Scripts\\python.exe run_testing.py 2024 --wf-window 5

Short WR optimization — override lifetime win rates with a custom file:
    .\\venv\\Scripts\\python.exe run_testing.py 2023 --wf-window 5 --wr-file checkpoints/short_wr_opt/winrates_v1_wf_avg.json

Logs to logs/testing_<year>.log
Resumes automatically from last checkpoint.
Paper trades saved to data/trade_logs/paper_trades.csv
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# ── prevent Windows sleep (no-op on Linux/Mac) ────────────────────────────────
def _keep_awake():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)

def _allow_sleep():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


# ── logging ───────────────────────────────────────────────────────────────────
def _setup_logging(year: int, log_dir: Path | None = None) -> Path:
    if log_dir is None:
        log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"testing_{year}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return log_file


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Usage: python run_testing.py <year|all> [--wf-window N] [--no-pre-filter]")
        print("  e.g: python run_testing.py 2023")
        print("       python run_testing.py 2019 --wf-window 1")
        print("       python run_testing.py all")
        sys.exit(1)

    year_arg   = sys.argv[1]
    use_filter = "--no-pre-filter" not in sys.argv

    # Parse optional --wf-window N
    wf_window = None
    if "--wf-window" in sys.argv:
        idx = sys.argv.index("--wf-window")
        try:
            wf_window = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("Error: --wf-window requires a window number (1-5).")
            sys.exit(1)

    # Parse optional --log-dir PATH
    log_dir = None
    if "--log-dir" in sys.argv:
        idx = sys.argv.index("--log-dir")
        try:
            log_dir = Path(sys.argv[idx + 1])
        except IndexError:
            print("Error: --log-dir requires a directory path.")
            sys.exit(1)

    # Parse optional --wr-file PATH
    wr_file = None
    if "--wr-file" in sys.argv:
        idx = sys.argv.index("--wr-file")
        try:
            wr_file = Path(sys.argv[idx + 1])
            if not wr_file.exists():
                print(f"Error: WR file not found: {wr_file}")
                sys.exit(1)
        except IndexError:
            print("Error: --wr-file requires a file path.")
            sys.exit(1)

    project_root = Path(__file__).parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from config.settings import TESTING_START_YEAR, TESTING_END_YEAR, PAPER_TRADES_FILE

    # Resolve WF frozen weights file
    wf_weights_file = None
    if wf_window is not None:
        checkpoint_dir  = project_root / "checkpoints"
        wf_weights_file = checkpoint_dir / f"wf{wf_window}_weights.json"
        if not wf_weights_file.exists():
            print(f"Error: WF-{wf_window} weights file not found: {wf_weights_file}")
            print(f"       Run 'python scripts/wf_freeze.py --window {wf_window}' first.")
            sys.exit(1)

    if year_arg == "all":
        years = list(range(TESTING_START_YEAR, TESTING_END_YEAR + 1))
    else:
        try:
            years = [int(year_arg)]
        except ValueError:
            print(f"Error: '{year_arg}' is not a valid year.")
            sys.exit(1)

    _setup_logging(years[0] if len(years) == 1 else 0, log_dir=log_dir)
    log = logging.getLogger(__name__)

    mode = f"WF-{wf_window} frozen weights" if wf_window else "live frozen weights (phase 2)"
    log.info("=" * 60)
    log.info(f"TESTING — {mode}, 1 trade/day")
    log.info(f"Years       : {years}")
    log.info(f"Pre-filter  : {'ON' if use_filter else 'OFF'}")
    log.info(f"Log dir     : {log_dir or Path(__file__).parent / 'logs'}")
    log.info(f"Paper trades: {PAPER_TRADES_FILE}")
    if wf_weights_file:
        log.info(f"WF weights  : {wf_weights_file}")
    if wr_file:
        log.info(f"WR override : {wr_file}")
    log.info("=" * 60)

    _keep_awake()
    log.info("Windows sleep prevention: ON")

    try:
        from backtester.engine import run_year

        for year in years:
            log.info(f"\n{'='*50}")
            log.info(f"Starting testing year: {year}")
            log.info(f"{'='*50}")

            summary = run_year(year=year, use_pre_filter=use_filter,
                               wf_weights_file=wf_weights_file,
                               wr_file=wr_file)

            t   = summary["total_trades"]
            ew  = summary["exact_wins"]
            pe  = summary["profitable_exits"]
            lo  = summary["losses"]
            ewr = summary["effective_win_rate"]
            xwr = summary["exact_win_rate"]
            pnl = summary["total_pnl"]
            lt  = summary.get("long_trades", 0)
            st  = summary.get("short_trades", 0)
            lp  = summary.get("long_pnl", 0.0)
            sp  = summary.get("short_pnl", 0.0)

            log.info(f"\n--- {year} RESULTS ---")
            log.info(f"Total trades   : {t}  ({lt} LONG, {st} SHORT)")
            log.info(f"EXACT_WIN      : {ew}  ({xwr}%)   — target hit")
            log.info(f"WIN (partial)  : {pe}  ({round(pe/t*100,1) if t else 0}%)   — profitable TIME_EXIT")
            log.info(f"LOSS           : {lo}  ({round(lo/t*100,1) if t else 0}%)   — stopped or exit at loss")
            log.info(f"Effective WR   : {ewr}%  (EXACT_WIN + WIN)")
            log.info(f"Total P&L      : Rs {pnl:,.0f}  (Long Rs {lp:,.0f} | Short Rs {sp:,.0f})")
            log.info(f"Paper trades   : {PAPER_TRADES_FILE}")

            # Update wf_results.json with test outcome if running WF mode
            if wf_window is not None:
                _update_wf_results(wf_window, year, pnl, ewr, t)

    except KeyboardInterrupt:
        log.info("Interrupted by user — checkpoint saved, safe to resume.")
    except Exception as e:
        log.exception(f"Unexpected error: {e}")
        sys.exit(1)
    finally:
        _allow_sleep()
        log.info("Windows sleep prevention: OFF")


def _update_wf_results(wf_window: int, year: int, pnl: float,
                        win_rate: float, trades: int) -> None:
    """Append test-year results into wf_results.json for the given WF window."""
    import json
    wf_results_file = Path(__file__).parent / "checkpoints" / "wf_results.json"
    results = {}
    if wf_results_file.exists():
        with open(wf_results_file) as f:
            results = json.load(f)

    key = f"WF-{wf_window}"
    if key not in results:
        results[key] = {"window": wf_window}

    # Accumulate test years (WF-5 spans 2023-2026, so append per-year)
    test_years = results[key].setdefault("test_years", {})
    test_years[str(year)] = {"pnl": round(pnl, 2), "win_rate": win_rate, "trades": trades}

    # Aggregate totals across all test years in this window
    all_pnl    = sum(v["pnl"]    for v in test_years.values())
    all_trades = sum(v["trades"] for v in test_years.values())
    avg_wr     = (round(sum(v["win_rate"] for v in test_years.values()) / len(test_years), 1)
                  if test_years else 0.0)
    results[key]["test_pnl"]    = round(all_pnl, 2)
    results[key]["test_trades"] = all_trades
    results[key]["test_win_rate"] = avg_wr
    results[key]["gate_pass"]   = bool(all_pnl > 0)

    with open(wf_results_file, "w") as f:
        json.dump(results, f, indent=2)

    gate = "PASS ✓" if all_pnl > 0 else "FAIL ✗"
    log = logging.getLogger(__name__)
    log.info(f"WF-{wf_window} running total: P&L Rs {all_pnl:,.0f} [{gate}] — "
             f"wf_results.json updated")


if __name__ == "__main__":
    main()
