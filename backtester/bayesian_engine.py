"""
B2g — Bayesian backtest engine (plan_phase1.md §4 B2)

A clean Bayesian day-by-day engine that replaces the old fixed-weight entry logic.
Reuses the old engine's data helpers; everything decision-related is Bayesian.

Per day:
  0. Macro event gate (SKIP / RAISE_THRESHOLD)
  1. Per stock: data-integrity gate -> generate 37 signals -> first-candle block ->
     last-entry cutoff
  2. Per direction pick the driver-eligible strategy (cluster not in E/F) with the
     highest Bayesian EV, run the four-gate entry decision (bayesian_gate)
  3. Across stocks take the highest-EV passing LONG and highest-EV passing SHORT
  4. Size (capped Kelly + portfolio caps), enforce 0.8%/day and the LONG/SHORT sector rule
  5. Execute (next-bar-open + slippage), record excursions
  6. Update ONLY the driver's posterior (driver-only), decay + winsorize
  7. Log the signal line and append to paper_trades.csv (with excursion columns)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd
from tqdm import tqdm

from config.settings import (
    STOCKS_DIR, CAPITAL, MAX_DAILY_RISK, MAX_RISK_PER_TRADE, ROUND_RISK_TOLERANCE,
    PAPER_TRADES_FILE, BAYES_STATE_FILE,
)
from strategies import ALL_STRATEGIES
from backtester.bayesian_gate import evaluate_entry, cluster_of
from backtester.bayesian_sizer import size_trade, fit_daily_risk, sectors_ok
from backtester.execution import simulate_execution
from backtester.filters import first_candle_filter, after_last_entry, event_day, event_mode
from backtester.universe import data_integrity_ok, fno_eligible_short, long_eligible, sector_of
from backtester.cost_model import net_pnl
from weights.bayesian import BayesianState
from backtester import engine as _eng   # reuse data helpers

log = logging.getLogger(__name__)

DRIVER_INELIGIBLE_CLUSTERS = {"E", "F"}   # context/meta accumulate no evidence -> not drivers


@dataclass
class Candidate:
    symbol: str
    driver: object          # Signal
    gate: object            # GateResult
    signals: dict
    ev: float


def _pick_driver(symbol, signals, bayes, direction, is_event_day) -> Candidate | None:
    """Highest-EV driver-eligible strategy firing in `direction` that passes the gate."""
    best: Candidate | None = None
    for name, sig in signals.items():
        if sig is None or sig.direction != direction or not sig.is_valid:
            continue
        if cluster_of(name) in DRIVER_INELIGIBLE_CLUSTERS:
            continue
        gate = evaluate_entry(sig, signals, bayes, is_event_day=is_event_day)
        if not gate.passed:
            continue
        # rank by execution-discounted EV (exec/context = 1.0 in Phase 1 -> plain EV)
        if best is None or gate.ev > best.ev:
            best = Candidate(symbol, sig, gate, signals, gate.ev)
    return best


def _trailing_median(history_5min: pd.DataFrame, trade_date) -> float:
    hist = history_5min[history_5min["datetime"].dt.date < trade_date]
    if hist.empty:
        return 0.0
    daily = hist.groupby(hist["datetime"].dt.date)["close"].median().tail(5)
    return float(daily.median()) if not daily.empty else 0.0


def _process_day(trade_date: date, all_data, nifty_data, bayes: BayesianState,
                 use_pre_filter=True) -> dict:
    # 0. macro event gate
    is_event, events = event_day(trade_date)
    if is_event and event_mode() == "SKIP":
        log.info(f"[EVENT_DAY_SKIP: {','.join(events)}] {trade_date}")
        return {"date": str(trade_date), "recommendations": [], "skipped": "event"}
    raise_thr = is_event and event_mode() == "RAISE_THRESHOLD"

    scan_data = all_data
    long_cands: list[Candidate] = []
    short_cands: list[Candidate] = []

    for symbol, df in scan_data.items():
        today = _eng._get_today(df, trade_date)
        if today.empty or len(today) < 10:
            continue
        history = df[df["datetime"].dt.date < trade_date]

        # data-integrity gate (glitch guard)
        today_med = float(today["close"].median())
        if not data_integrity_ok(today_med, _trailing_median(df, trade_date), symbol, trade_date):
            continue

        prev = _eng._get_prev_day_ohlc(history, trade_date)

        signals = {}
        for strat in ALL_STRATEGIES:
            try:
                signals[strat.name] = strat.generate_signal(
                    today_5min=today, history_5min=history, prev_day=prev,
                    nifty_today=today, trade_date=trade_date)
            except Exception:
                from strategies.base import Signal
                signals[strat.name] = Signal(strat.name, 0)

        # first-candle block: drop non-exempt strategies that fired at 09:15
        signals = {n: s for n, s in signals.items()
                   if not (s and s.direction != 0 and s.signal_time == "09:15"
                           and n not in first_candle_filter({n: s}, "09:15"))}
        # last-entry cutoff: no NEW entries at/after 14:30
        signals = {n: s for n, s in signals.items()
                   if not (s and s.direction != 0 and s.signal_time and after_last_entry(s.signal_time))}

        long_d = _pick_driver(symbol, signals, bayes, +1, raise_thr)
        if long_d and long_eligible(symbol, trade_date):
            long_cands.append(long_d)
        short_d = _pick_driver(symbol, signals, bayes, -1, raise_thr)
        if short_d and fno_eligible_short(symbol, trade_date):
            short_cands.append(short_d)

    # take highest-EV LONG + highest-EV SHORT
    best_long = max(long_cands, key=lambda c: c.ev, default=None)
    best_short = max(short_cands, key=lambda c: c.ev, default=None)

    turnover = _eng._estimate_turnover(all_data, trade_date)   # crores
    recs = []
    risk_used = 0.0
    # size the higher-EV side first so the daily-risk cap favours the better trade
    ordered = sorted([c for c in (best_long, best_short) if c], key=lambda c: -c.ev)
    long_sector = short_sector = None
    for cand in ordered:
        rec = _build_trade(cand, all_data, trade_date, bayes, turnover, risk_used,
                           long_sector, short_sector)
        if rec is None:
            continue
        risk_used += rec["intended_risk"]
        if cand.driver.direction > 0:
            long_sector = rec["sector"]
        else:
            short_sector = rec["sector"]
        recs.append(rec)

    return {"date": str(trade_date), "recommendations": recs}


def _build_trade(cand: Candidate, all_data, trade_date, bayes, turnover,
                 risk_used, long_sector, short_sector) -> dict | None:
    sig = cand.driver
    gate = cand.gate
    direction = sig.direction
    post = bayes.get_posterior(sig.strategy, direction)

    # sector rule (skipped when the map is absent)
    sector = sector_of(cand.symbol, trade_date)
    other = short_sector if direction > 0 else long_sector
    if sector and other and not sectors_ok(
            sector if direction > 0 else other, other if direction > 0 else sector):
        log.info(f"[SECTOR_RULE_SKIP {cand.symbol} {sector}]")
        return None

    adv_rs = turnover.get(cand.symbol, 0.0) * 1e7
    r = size_trade(sig.entry, sig.stop, sig.rr, gate.ev, post.posterior_scale, gate.gate_mult,
                   adv_turnover_rs=adv_rs, available_cash=CAPITAL, burn_in=gate.burn_in)
    if not r.ok:
        log.info(f"[{r.skip_reason.upper()}] {cand.symbol} {sig.strategy} flags={r.flags}")
        return None

    # enforce 0.8%/day: shrink intended risk to headroom; skip if none
    fitted = fit_daily_risk(r.intended_risk, risk_used)
    if fitted <= 0:
        log.info(f"[DAILY_RISK_FULL] {cand.symbol} skipped")
        return None
    scale = fitted / r.intended_risk if r.intended_risk > 0 else 0.0
    shares = max(0, int(r.shares * scale))
    if shares <= 0:
        return None

    today = _eng._get_today(all_data[cand.symbol], trade_date)
    ex = simulate_execution(sig, today, shares=shares)
    if not ex.filled:
        return None
    pnl = net_pnl(ex.entry_fill, ex.exit_price, shares, direction=direction)
    actual_risk = shares * abs(ex.entry_fill - sig.stop)

    # Realized-risk cap: the next-bar-open fill can gap far from the signal-level stop
    # (e.g. a squeeze release sized on a tiny stop), realizing more risk than intended.
    # The hard 0.5%/trade cap holds on REALIZED risk — skip a fill that breaches it.
    if actual_risk > MAX_RISK_PER_TRADE * CAPITAL + 1e-6:
        log.info(f"[RISK_CAP_SKIP {cand.symbol} {sig.strategy} fill-gap "
                 f"realized={actual_risk / CAPITAL * 100:.2f}%]")
        return None

    # driver-only posterior update
    bayes.update(sig.strategy, direction, pnl_rs=pnl, risk_amount=actual_risk, rr=sig.rr)

    log.info("TRADE %s %-12s %-5s %s", trade_date, cand.symbol,
             "LONG" if direction > 0 else "SHORT", gate.log_line(cand.symbol, sig.strategy))

    return {
        "date": str(trade_date), "symbol": cand.symbol,
        "direction": "LONG" if direction > 0 else "SHORT",
        "driver_strategy": sig.strategy, "signal_time": sig.signal_time,
        "entry_price": ex.entry_fill, "quantity": shares, "position_rs": round(shares * ex.entry_fill, 2),
        "stop_loss": sig.stop, "target": sig.target, "rr": sig.rr,
        "ev": round(gate.ev, 4), "driver_mu": round(gate.driver_mu, 4),
        "driver_p": round(gate.driver_p, 4), "gate_mult": gate.gate_mult,
        "eff_binary": gate.clusters.eff_binary, "eff_weighted": gate.clusters.eff_weighted,
        "clusters_confirmed": "".join(sorted(gate.clusters.confirmed)),
        "clusters_contradicting": "".join(sorted(gate.clusters.contradicting)),
        "cf_contra": gate.cf_contra, "sector": sector or "",
        "intended_risk": r.intended_risk, "actual_risk": round(actual_risk, 2),
        "risk_pct": round(actual_risk / CAPITAL * 100, 4),
        "exit_time": ex.exit_time, "exit_price": ex.exit_price, "exit_reason": ex.exit_reason,
        "mfe_r": ex.mfe_r, "mae_r": ex.mae_r, "bars_to_exit": ex.bars_to_exit,
        "settings_hash": ex.settings_hash, "pnl_rs": round(pnl, 2),
        "sizer_flags": ",".join(r.flags),
    }


def run_year_bayesian(year: int, bayes: BayesianState | None = None,
                      paper_file=None, save_state=True, days_limit: int | None = None) -> dict:
    """Run one year through the Bayesian engine. Posteriors carry across days (and years)."""
    bayes = bayes if bayes is not None else BayesianState.load()
    paper_file = paper_file or PAPER_TRADES_FILE

    all_data, nifty_data = _eng._preload_data(year)
    trading_days = _eng._get_trading_days(all_data, year)
    if days_limit:
        trading_days = trading_days[:days_limit]

    all_recs = []
    for td in tqdm(trading_days, desc=f"Bayes {year}", unit="day"):
        day = _process_day(td, all_data, nifty_data, bayes)
        recs = day.get("recommendations", [])
        all_recs.extend(recs)
        if recs:
            _append_paper(recs, paper_file)

    if save_state:
        bayes.save(BAYES_STATE_FILE)

    n = len(all_recs)
    wins = sum(1 for r in all_recs if r["pnl_rs"] > 0)
    total_pnl = sum(r["pnl_rs"] for r in all_recs)
    avg_ev = (sum(r["ev"] for r in all_recs) / n) if n else 0.0
    max_risk_pct = max((r["risk_pct"] for r in all_recs), default=0.0)
    summary = {
        "year": year, "trades": n, "wins": wins,
        "win_rate": round(wins / n * 100, 1) if n else 0.0,
        "total_pnl": round(total_pnl, 0), "avg_ev": round(avg_ev, 3),
        "max_trade_risk_pct": round(max_risk_pct, 4),
    }
    log.info(f"[BAYES {year}] {summary}")
    return summary


def _append_paper(recs: list[dict], paper_file) -> None:
    df = pd.DataFrame(recs)
    paper_file.parent.mkdir(parents=True, exist_ok=True)
    header = not paper_file.exists()
    df.to_csv(paper_file, mode="a", header=header, index=False)
