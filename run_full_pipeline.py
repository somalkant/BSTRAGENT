"""
Full Walk-Forward Pipeline — runs the entire 2016-2026 backtest automatically.

WF schedule:
  Train 2016-2018 → freeze WF1 → test 2019
  Train 2019      → freeze WF2 → test 2020
  Train 2020      → freeze WF3 → test 2021
  Train 2021      → freeze WF4 → test 2022
  Train 2022      → freeze WF5 → test 2023-2026  ← LIVE WEIGHTS

Outputs (used by live agent):
  checkpoints/strategy_weights.json        — final WF5 weights (long + short per strategy)
  checkpoints/strategy_lifetime_winrates.json — lifetime win rates (agreement filter + conviction)

Runtime: ~3-4 days on t3.xlarge. Safe to Ctrl+C and resume — each year checkpoints progress.

Usage:
    python run_full_pipeline.py              # full run from the beginning
    python run_full_pipeline.py --from 2020  # resume from a specific year (skip earlier)
    python run_full_pipeline.py --dry-run    # print the plan without executing
"""

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Full WF pipeline — each step is (type, year_or_years, wf_window_or_None)
# type: "train" | "freeze" | "test"
PIPELINE = [
    ("train",  2016, None),
    ("train",  2017, None),
    ("train",  2018, None),
    ("freeze", None,   1),   # WF1: trained on 2016-2018
    ("test",   2019,   1),
    ("train",  2019, None),
    ("freeze", None,   2),   # WF2: trained on 2016-2019
    ("test",   2020,   2),
    ("train",  2020, None),
    ("freeze", None,   3),   # WF3: trained on 2016-2020
    ("test",   2021,   3),
    ("train",  2021, None),
    ("freeze", None,   4),   # WF4: trained on 2016-2021
    ("test",   2022,   4),
    ("train",  2022, None),
    ("freeze", None,   5),   # WF5: final live weights (trained 2016-2022)
    ("test",   2023,   5),
    ("test",   2024,   5),
    ("test",   2025,   5),
    ("test",   2026,   5),
]


def _label(step) -> str:
    kind, year, wf = step
    if kind == "train":  return f"TRAIN {year}"
    if kind == "freeze": return f"FREEZE WF-{wf}"
    if kind == "test":   return f"TEST  {year}  [WF-{wf}]"


def _run(cmd: list[str], log_file: Path) -> int:
    """Run a subprocess, tee-ing stdout+stderr to both console and log file."""
    log.info(f"  $ {' '.join(cmd)}")
    with open(log_file, "a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            sys.stdout.write(line)
            lf.write(line)
        proc.wait()
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="Full WF pipeline runner")
    parser.add_argument("--from", dest="from_year", type=int, default=None,
                        help="Skip all steps before this training year")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan without executing")
    args = parser.parse_args()

    # ── logging ───────────────────────────────────────────────────────────────
    pipeline_log = LOG_DIR / f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(pipeline_log, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    global log
    log = logging.getLogger(__name__)

    python = str(BASE_DIR / "venv" / "bin" / "python")
    if not Path(python).exists():
        # Windows fallback
        python = str(BASE_DIR / "venv" / "Scripts" / "python.exe")
    if not Path(python).exists():
        python = sys.executable   # use whatever python is running this script

    # ── dry run ───────────────────────────────────────────────────────────────
    if args.dry_run:
        print("\nFull WF pipeline — execution plan:")
        print(f"{'Step':<5}  {'Action':<25}  {'Log'}")
        print("─" * 65)
        for i, step in enumerate(PIPELINE, 1):
            kind, year, wf = step
            log_name = (f"analysis_{year}.log" if kind == "train"
                        else f"testing_{year}.log" if kind == "test"
                        else f"freeze_wf{wf}.log")
            skip = " ← SKIP" if args.from_year and kind == "train" and year < args.from_year else ""
            print(f"{i:<5}  {_label(step):<25}  {log_name}{skip}")
        print()
        return

    log.info("=" * 65)
    log.info("FULL WF PIPELINE STARTING")
    log.info(f"Pipeline log: {pipeline_log}")
    log.info(f"Python      : {python}")
    log.info("=" * 65)

    t_start = datetime.now()
    steps_done = 0

    for i, step in enumerate(PIPELINE, 1):
        kind, year, wf = step

        # ── skip logic ────────────────────────────────────────────────────────
        if args.from_year and kind == "train" and year < args.from_year:
            log.info(f"[{i}/{len(PIPELINE)}] SKIP {_label(step)} (--from {args.from_year})")
            continue
        # If we're skipping training years, also skip freezes for already-done WF windows.
        # The checkpoint file is the guard — if it exists, skip the freeze.
        if kind == "freeze":
            frozen = BASE_DIR / "checkpoints" / f"wf{wf}_weights.json"
            if frozen.exists() and args.from_year:
                log.info(f"[{i}/{len(PIPELINE)}] SKIP {_label(step)} ({frozen.name} already exists)")
                continue

        log.info("")
        log.info(f"[{i}/{len(PIPELINE)}] ── {_label(step)} ──────────────────────────────────")
        step_start = datetime.now()

        # ── build command ─────────────────────────────────────────────────────
        if kind == "train":
            step_log = LOG_DIR / f"analysis_{year}.log"
            cmd = [python, "run_analysis.py", str(year)]

        elif kind == "freeze":
            step_log = LOG_DIR / f"freeze_wf{wf}.log"
            cmd = [python, "scripts/wf_freeze.py", "--window", str(wf)]

        elif kind == "test":
            step_log = LOG_DIR / f"testing_{year}_wf{wf}.log"
            cmd = [python, "run_testing.py", str(year), "--wf-window", str(wf)]

        # ── execute ───────────────────────────────────────────────────────────
        rc = _run(cmd, step_log)
        elapsed = datetime.now() - step_start

        if rc != 0:
            log.error(f"[{i}] FAILED with exit code {rc} after {elapsed}")
            log.error(f"     Check log: {step_log}")
            log.error("Pipeline halted. Fix the error and resume with --from <year>")
            sys.exit(rc)

        log.info(f"[{i}] DONE in {str(elapsed).split('.')[0]}  →  log: {step_log.name}")
        steps_done += 1

    # ── final summary ─────────────────────────────────────────────────────────
    total = datetime.now() - t_start
    log.info("")
    log.info("=" * 65)
    log.info("PIPELINE COMPLETE")
    log.info(f"Total time : {str(total).split('.')[0]}")
    log.info(f"Steps done : {steps_done}/{len(PIPELINE)}")
    log.info("")
    log.info("Outputs for live agent:")
    log.info("  checkpoints/strategy_weights.json          ← WF5 weights (long+short)")
    log.info("  checkpoints/strategy_lifetime_winrates.json ← win rates for agreement filter")
    log.info("  checkpoints/wf_results.json                ← which WF windows passed gate")
    log.info("=" * 65)

    # ── upload results to S3 so local machine can pull them ──────────────────
    _upload_results_to_s3(python, pipeline_log)


def _upload_results_to_s3(python: str, pipeline_log: Path) -> None:
    """Sync checkpoints/ and logs/ back to S3 so results are accessible without SSH."""
    import shutil
    log.info("")
    log.info("Uploading results to S3...")

    bucket = "amzn-s3-somal-bucket"
    prefix = "tradingagent"

    # Use aws cli (already installed on EC2 via ec2_setup.sh)
    aws = shutil.which("aws")
    if not aws:
        log.warning("aws CLI not found — skipping S3 upload. Copy checkpoints/ manually via SCP.")
        log.warning("  scp -i your-key.pem ubuntu@<EC2-IP>:~/TRAIAGENT/checkpoints/ .")
        return

    dirs = [
        (str(BASE_DIR / "checkpoints"), f"s3://{bucket}/{prefix}/checkpoints/"),
        (str(LOG_DIR),                  f"s3://{bucket}/{prefix}/logs/"),
    ]
    for local_dir, s3_uri in dirs:
        cmd = [aws, "s3", "sync", local_dir, s3_uri, "--exclude", "access_token.json"]
        log.info(f"  aws s3 sync {local_dir.split('/')[-1]}/ → {s3_uri}")
        rc = _run(cmd, pipeline_log)
        if rc != 0:
            log.warning(f"  S3 upload failed for {local_dir} (exit {rc}) — results still on EC2")

    log.info("")
    log.info("Results uploaded. Pull to your local machine with:")
    log.info(f"  aws s3 sync s3://{bucket}/{prefix}/checkpoints/ checkpoints/")
    log.info(f"  aws s3 sync s3://{bucket}/{prefix}/logs/        logs/ec2/")


if __name__ == "__main__":
    main()
