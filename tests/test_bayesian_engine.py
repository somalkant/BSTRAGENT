"""
B2g integration — Bayesian engine end-to-end on real data (plan_phase1.md §4 B2).

Runs a tiny slice (few stocks, few days) and asserts the pipeline invariants.
Skips if the stock data store is not available.

Run:  python -m pytest tests/test_bayesian_engine.py -v
"""
from __future__ import annotations

import pandas as pd
import pytest

from config.settings import STOCKS_DIR, DAILY_RISK_CAP_RS
from weights.bayesian import BayesianState
import backtester.bayesian_engine as be

SYMS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN"]


def _load(year_warm, year):
    data = {}
    for s in SYMS:
        frames = []
        for y in (year_warm, year):
            f = STOCKS_DIR / str(y) / f"{s}.parquet"
            if f.exists():
                df = pd.read_parquet(f)
                df["datetime"] = pd.to_datetime(df["datetime"])
                frames.append(df)
        if frames:
            data[s] = (pd.concat(frames).drop_duplicates("datetime")
                       .sort_values("datetime").reset_index(drop=True))
    return data


@pytest.fixture(scope="module")
def slice_run():
    all_data = _load(2017, 2018)
    if len(all_data) < 3:
        pytest.skip("stock data store not available")
    days = be._eng._get_trading_days(all_data, 2018)[:8]
    bayes = BayesianState()
    recs = []
    for td in days:
        recs.extend(be._process_day(td, all_data, None, bayes).get("recommendations", []))
    return recs, bayes


def test_pipeline_produces_trades(slice_run):
    recs, _ = slice_run
    assert len(recs) >= 1                       # burn-in bootstraps trades from a clean start


def test_risk_cap_never_exceeded(slice_run):
    """Each trade risks at most its direction's flat daily budget (burn-in trades
    risk far less; see DAILY_RISK_CAP_RS in bayesian_sizer)."""
    recs, _ = slice_run
    for r in recs:
        assert r["actual_risk"] <= DAILY_RISK_CAP_RS + 1e-6


def test_excursion_and_integrity_fields_present(slice_run):
    recs, _ = slice_run
    need = {"mfe_r", "mae_r", "bars_to_exit", "exit_reason", "settings_hash",
            "eff_binary", "eff_weighted", "ev", "gate_mult"}
    for r in recs:
        assert need <= set(r)
        assert r["exit_reason"] in {"TARGET", "STOP", "EOD", "CIRCUIT_TRAP"}


def test_driver_only_update(slice_run):
    """Each trade updates exactly its driver's posterior; only drivers gain evidence."""
    recs, bayes = slice_run
    drivers = {(r["driver_strategy"], r["direction"].lower()) for r in recs}
    for strat, dirs in bayes._state.items():
        for d, cell in dirs.items():
            if cell.n_eff > 0:                  # got real evidence -> must have been a driver
                assert (strat, d) in drivers
