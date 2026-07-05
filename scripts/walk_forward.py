"""
B6 — Full Walk-Forward orchestrator (plan_phase3.md §B6, 19 steps)

Trains 2016..train_end, FREEZES a posterior snapshot (wf{N}_bayes.json), then tests the
next year with DECISIONS on the frozen snapshot while the live posterior keeps updating
from test outcomes (no separate re-run). Repeats for all 8 windows.

The 19-step run over the full universe is compute-heavy — this builds the orchestration;
trigger the actual run explicitly. --smoke runs a tiny slice to prove the mechanism.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import WF_WINDOWS, CHECKPOINT_DIR, LEARNING_START_YEAR   # noqa: E402
from weights.bayesian import BayesianState                                    # noqa: E402
from weights.stock_type import StockTypePrior                                 # noqa: E402
from weights.changepoint import ChangePointMonitor                            # noqa: E402
from weights.regime import RegimeClassifier                                   # noqa: E402
import backtester.bayesian_engine as be                                       # noqa: E402


def wf_snapshot_path(wf: int) -> Path:
    return CHECKPOINT_DIR / f"wf{wf}_bayes.json"


def _live_state_paths(wf: int) -> tuple[Path, Path]:
    return (CHECKPOINT_DIR / f"wf{wf}_live_bayes.json",
            CHECKPOINT_DIR / f"wf{wf}_live_stock_type.json")


def _last_completed_window(windows) -> dict | None:
    """Last window (in order) whose live-state checkpoints both exist. Windows are
    assumed to complete in order, so the scan stops at the first missing one."""
    last = None
    for w in windows:
        bayes_p, stock_p = _live_state_paths(w["wf"])
        if bayes_p.exists() and stock_p.exists():
            last = w
        else:
            break
    return last


def run_walk_forward(days_limit: int | None = None, windows=None,
                     max_stocks: int | None = None) -> list[dict]:
    """Execute the WF protocol. Returns a per-step summary list.

    Resumable at window granularity: once a window fully completes (train -> freeze ->
    test), its live bayes/stock_type state is saved as wf{N}_live_*.json. A fresh call
    scans for the last window whose live checkpoints exist and resumes right after it,
    instead of retraining from LEARNING_START_YEAR — a crash mid-window only costs that
    one window's work, not the whole run. The regime classifier's hysteresis buffer and
    the changepoint monitor's history are cheap to rebuild and are NOT preserved across
    a resume (they re-stabilize within a few days); only the learned strategy posteriors
    and stock-type priors — the expensive part — carry over.

    Resuming with different days_limit/max_stocks than the interrupted run produced its
    checkpoints under is not validated — keep those consistent across a resume.
    """
    windows = windows or WF_WINDOWS
    resume_from = _last_completed_window(windows)

    if resume_from is not None:
        wf0 = resume_from["wf"]
        bayes_p, stock_p = _live_state_paths(wf0)
        bayes = BayesianState.load(bayes_p)
        bayes.attach_changepoint(ChangePointMonitor())
        stock_type = StockTypePrior.load(stock_p)
        trained_through = resume_from["test"]
        remaining = [w for w in windows if w["wf"] > wf0]
        print(f"[WF] resuming after WF{wf0} (trained_through={trained_through}) — "
              f"{len(remaining)} window(s) remaining", flush=True)
    else:
        bayes = BayesianState()
        bayes.attach_changepoint(ChangePointMonitor())
        stock_type = StockTypePrior()
        trained_through = LEARNING_START_YEAR - 1
        remaining = windows

    classifier = RegimeClassifier()
    results = []

    for w in remaining:
        wf, train_end, test_year = w["wf"], w["train_end"], w["test"]

        # 1) train any not-yet-trained years up to train_end (posteriors update inline)
        for yr in range(trained_through + 1, train_end + 1):
            s = be.run_year_bayesian(yr, bayes=bayes, save_state=False,
                                     classifier=classifier, stock_type=stock_type,
                                     days_limit=days_limit, max_stocks=max_stocks,
                                     paper_file=CHECKPOINT_DIR / f"wf_train_{yr}.csv")
            results.append({"step": "train", "year": yr, **s})
        trained_through = max(trained_through, train_end)

        # 2) FREEZE — snapshot the decision posterior for the test year
        snap = wf_snapshot_path(wf)
        bayes.save(snap)
        frozen = BayesianState.load(snap)               # decisions use this; never updated
        results.append({"step": "freeze", "wf": wf, "snapshot": snap.name})

        # 3) TEST — decisions on frozen snapshot; live `bayes` keeps updating from outcomes
        s = be.run_year_bayesian(test_year, bayes=bayes, decision_bayes=frozen,
                                 save_state=False, classifier=classifier, stock_type=stock_type,
                                 days_limit=days_limit, max_stocks=max_stocks,
                                 paper_file=CHECKPOINT_DIR / f"wf{wf}_test_{test_year}.csv")
        results.append({"step": "test", "wf": wf, "year": test_year, **s})
        # the live posterior already absorbed test_year's outcomes inline above — the next
        # window's training loop must not re-walk it (plan_phase3.md B6: no re-run)
        trained_through = max(trained_through, test_year)

        # window fully complete -> persist resumable state
        bayes_p, stock_p = _live_state_paths(wf)
        bayes.save(bayes_p)
        stock_type.save(stock_p)
        results.append({"step": "checkpoint", "wf": wf})

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="tiny slice to prove the mechanism")
    ap.add_argument("--days-limit", type=int, default=None)
    ap.add_argument("--windows", type=int, nargs="+", default=None, help="subset of WF ids")
    args = ap.parse_args()

    windows = WF_WINDOWS
    if args.windows:
        windows = [w for w in WF_WINDOWS if w["wf"] in args.windows]
    days = 5 if args.smoke else args.days_limit

    print(f"[WF] running {len(windows)} window(s), days_limit={days}", flush=True)
    for r in run_walk_forward(days_limit=days, windows=windows):
        print("[WF]", r, flush=True)


if __name__ == "__main__":
    main()
