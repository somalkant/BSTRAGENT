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


def run_walk_forward(days_limit: int | None = None, windows=None) -> list[dict]:
    """Execute the WF protocol. Returns a per-step summary list."""
    windows = windows or WF_WINDOWS
    bayes = BayesianState()
    bayes.attach_changepoint(ChangePointMonitor())
    stock_type = StockTypePrior()
    classifier = RegimeClassifier()
    results = []

    trained_through = LEARNING_START_YEAR - 1
    for w in windows:
        wf, train_end, test_year = w["wf"], w["train_end"], w["test"]

        # 1) train any not-yet-trained years up to train_end (posteriors update inline)
        for yr in range(trained_through + 1, train_end + 1):
            s = be.run_year_bayesian(yr, bayes=bayes, save_state=False,
                                     classifier=classifier, stock_type=stock_type,
                                     days_limit=days_limit,
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
                                 days_limit=days_limit,
                                 paper_file=CHECKPOINT_DIR / f"wf{wf}_test_{test_year}.csv")
        results.append({"step": "test", "wf": wf, "year": test_year, **s})

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
