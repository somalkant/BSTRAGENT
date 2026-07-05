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
import time
from dataclasses import dataclass
from datetime import date

import pandas as pd

from config.settings import (
    STOCKS_DIR, CAPITAL, MAX_DAILY_RISK, MAX_RISK_PER_TRADE, ROUND_RISK_TOLERANCE,
    PAPER_TRADES_FILE, BAYES_STATE_FILE, DEFAULT_REGIME,
    VIX_BAND_LO_PCTILE, VIX_BAND_HI_PCTILE, ADX_THRESHOLD, ADX_BAND,
)
from strategies import ALL_STRATEGIES
from backtester.bayesian_gate import evaluate_entry, cluster_of
from backtester.bayesian_sizer import size_trade, fit_daily_risk, sectors_ok
from backtester.execution import simulate_execution
from backtester.filters import first_candle_filter, after_last_entry, event_day, event_mode
from backtester.universe import data_integrity_ok, fno_eligible_short, long_eligible, sector_of
from backtester.exec_quality import compute as exec_compute
from backtester.context_tags import compute_tags, context_mult, signal_label, time_bucket
from backtester.cost_model import net_pnl
from weights.bayesian import BayesianState
from weights.regime import RegimeClassifier
from weights.stock_type import StockTypePrior
from weights.changepoint import ChangePointMonitor
from backtester import engine as _eng   # reuse data helpers

log = logging.getLogger(__name__)

DRIVER_INELIGIBLE_CLUSTERS = {"E", "F"}   # context/meta accumulate no evidence -> not drivers

# B5b short-universe impact counter: gated SHORTs dropped only for point-in-time F&O
# ineligibility — measures how much short opportunity the broker constraint costs.
SHORT_UNIVERSE_COUNTER = {"gated_shorts": 0, "outside_fno": 0}


def reset_short_universe_counter() -> None:
    SHORT_UNIVERSE_COUNTER.update(gated_shorts=0, outside_fno=0)


@dataclass
class DayContext:
    regime:    str = DEFAULT_REGIME
    vix:       float = 15.0
    adx:       float = 20.0
    bands:     dict = None
    breadth:   float | None = None
    is_event:  bool = False


@dataclass
class Candidate:
    symbol: str
    driver: object          # Signal
    gate: object            # GateResult
    signals: dict
    exec_q: object          # ExecQuality
    disc_ev: float          # execution-discounted EV (ranking key)


def _pick_driver(symbol, signals, bayes, direction, ctx: DayContext, prev, today) -> Candidate | None:
    """Highest exec-discounted-EV driver-eligible strategy in `direction` passing the gate.
    `bayes` here is the DECISION state (frozen during a WF test year)."""
    best: Candidate | None = None
    is_event = ctx.is_event
    for name, sig in signals.items():
        if sig is None or sig.direction != direction or not sig.is_valid:
            continue
        cl = cluster_of(name)
        if cl in DRIVER_INELIGIBLE_CLUSTERS:
            continue
        gate = evaluate_entry(sig, signals, bayes, is_event_day=is_event, regime=ctx.regime)
        if not gate.passed:
            continue
        # B4e execution quality (L3 Trigger) — veto/skip and exec_mult
        eq = exec_compute(sig, today, prev, cl)
        if eq.veto or eq.skip:
            log.debug(f"{eq.log_line()} {symbol} {name}")
            continue
        cm = context_mult(direction, ctx.breadth)
        disc_ev = gate.ev * eq.exec_mult * cm       # ranking and sizing must agree
        if best is None or disc_ev > best.disc_ev:
            best = Candidate(symbol, sig, gate, signals, eq, disc_ev)
    return best


def _trailing_median(history: pd.DataFrame) -> float:
    """history: already filtered to dates < trade_date (see _process_day) — do not
    re-filter the full multi-year frame here, that's the expensive part."""
    if history.empty:
        return 0.0
    daily = history.groupby(history["datetime"].dt.date)["close"].median().tail(5)
    return float(daily.median()) if not daily.empty else 0.0


def _breadth(all_data, trade_date, hhmm: str) -> float | None:
    """Fraction of scanned stocks up (open -> price at hhmm). Lookahead-free (bars <= hhmm)."""
    up = tot = 0
    for df in all_data.values():
        t = df[df["datetime"].dt.date == trade_date]
        t = t[t["datetime"].dt.strftime("%H:%M") <= hhmm]
        if len(t) < 1:
            continue
        op = float(t.iloc[0]["open"]); px = float(t.iloc[-1]["close"])
        if op > 0:
            tot += 1
            up += 1 if px > op else 0
    return (up / tot) if tot else None


def _process_day(trade_date: date, all_data, nifty_data, bayes: BayesianState,
                 ctx: DayContext | None = None, stock_type: StockTypePrior | None = None,
                 decision_bayes: BayesianState | None = None) -> dict:
    ctx = ctx if ctx is not None else DayContext()
    stock_type = stock_type if stock_type is not None else StockTypePrior()
    decision_bayes = decision_bayes if decision_bayes is not None else bayes   # frozen in WF test
    # 0. macro event gate
    is_event, events = event_day(trade_date)
    if is_event and event_mode() == "SKIP":
        log.info(f"[EVENT_DAY_SKIP: {','.join(events)}] {trade_date}")
        return {"date": str(trade_date), "recommendations": [], "skipped": "event"}
    if is_event and event_mode() == "RAISE_THRESHOLD":
        ctx.is_event = True                # raised bars in _pick_driver/evaluate_entry

    long_cands: list[Candidate] = []
    short_cands: list[Candidate] = []

    for symbol, df in all_data.items():
        today = _eng._get_today(df, trade_date)
        if today.empty or len(today) < 10:
            continue
        history = df[df["datetime"].dt.date < trade_date]

        today_med = float(today["close"].median())
        if not data_integrity_ok(today_med, _trailing_median(history), symbol, trade_date):
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

        signals = {n: s for n, s in signals.items()
                   if not (s and s.direction != 0 and s.signal_time == "09:15"
                           and n not in first_candle_filter({n: s}, "09:15"))}
        signals = {n: s for n, s in signals.items()
                   if not (s and s.direction != 0 and s.signal_time and after_last_entry(s.signal_time))}

        long_d = _pick_driver(symbol, signals, decision_bayes, +1, ctx, prev, today)
        if long_d and long_eligible(symbol, trade_date):
            long_cands.append(long_d)
        short_d = _pick_driver(symbol, signals, decision_bayes, -1, ctx, prev, today)
        if short_d:
            SHORT_UNIVERSE_COUNTER["gated_shorts"] += 1
            if fno_eligible_short(symbol, trade_date):
                short_cands.append(short_d)
            else:
                SHORT_UNIVERSE_COUNTER["outside_fno"] += 1
                log.info(f"[SHORT_OUTSIDE_FNO {symbol} {short_d.driver.strategy}]")

    best_long = max(long_cands, key=lambda c: c.disc_ev, default=None)
    best_short = max(short_cands, key=lambda c: c.disc_ev, default=None)

    turnover = _eng._estimate_turnover(all_data, trade_date)
    recs = []
    risk_used = 0.0
    ordered = sorted([c for c in (best_long, best_short) if c], key=lambda c: -c.disc_ev)
    long_sector = short_sector = None
    for cand in ordered:
        # breadth as of this driver's signal time (context_mult input; log tag)
        ctx.breadth = _breadth(all_data, trade_date, cand.driver.signal_time or "09:15")
        rec = _build_trade(cand, all_data, trade_date, bayes, turnover, risk_used,
                           long_sector, short_sector, ctx, stock_type, decision_bayes)
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
                 risk_used, long_sector, short_sector, ctx: DayContext,
                 stock_type: StockTypePrior, decision_bayes: BayesianState | None = None) -> dict | None:
    sig = cand.driver
    gate = cand.gate
    eq = cand.exec_q
    direction = sig.direction
    decision_bayes = decision_bayes if decision_bayes is not None else bayes
    post = decision_bayes.get_posterior(sig.strategy, direction, ctx.regime)   # sizing uses frozen state

    # sector rule (skipped when the map is absent)
    sector = sector_of(cand.symbol, trade_date)
    other = short_sector if direction > 0 else long_sector
    if sector and other and not sectors_ok(
            sector if direction > 0 else other, other if direction > 0 else sector):
        log.info(f"[SECTOR_RULE_SKIP {cand.symbol} {sector}]")
        return None

    cm = context_mult(direction, ctx.breadth)             # B4f: the one context sizing rule
    adv_rs = turnover.get(cand.symbol, 0.0) * 1e7
    r = size_trade(sig.entry, sig.stop, sig.rr, gate.ev, post.posterior_scale, gate.gate_mult,
                   adv_turnover_rs=adv_rs, available_cash=CAPITAL, burn_in=gate.burn_in,
                   exec_mult=eq.exec_mult, context_mult=cm)   # B4e/B4f full sizing chain
    if not r.ok:
        cause = " cause=exec_mult" if "LOT_ROUND_SKIP" in r.flags and eq.exec_mult < 1.0 else ""
        log.info(f"[{r.skip_reason.upper()}{cause}] {cand.symbol} {sig.strategy} flags={r.flags}")
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

    # settled trade -> compute the [0,1] evidence score, update driver posterior (with
    # regime tag) and the per-stock behavior prior (B3b), and run change-point detection
    score, _, _ = bayes.score_from_pnl(pnl, actual_risk, sig.rr)
    bayes.update(sig.strategy, direction, pnl_rs=pnl, risk_amount=actual_risk, rr=sig.rr,
                 regime=ctx.regime)
    dcluster = cluster_of(sig.strategy)
    stock_type.update(cand.symbol, dcluster, score)

    # B4f trade tags + pre-registered signal-level outcome label (log-only)
    today_bars = _eng._get_today(all_data[cand.symbol], trade_date)
    history = all_data[cand.symbol][all_data[cand.symbol]["datetime"].dt.date < trade_date]
    prev = _eng._get_prev_day_ohlc(history, trade_date)
    tags = compute_tags(sig, today_bars, prev, history, breadth=ctx.breadth)
    from backtester.exec_quality import _atr as _atr_of
    label = signal_label(sig, today_bars, _atr_of(today_bars[
        today_bars["datetime"].dt.strftime("%H:%M") <= (sig.signal_time or "09:15")]))

    outcome = (f"entry={ex.entry_fill:.2f}@{ex.entry_time} exit={ex.exit_price:.2f}@{ex.exit_time} "
               f"{ex.exit_reason:<12} size={shares}sh (Rs {shares * ex.entry_fill:,.0f}) "
               f"P&L Rs {pnl:+,.0f}")
    log.info("TRADE %s %-12s %-5s regime=%s %s %s | %s", trade_date, cand.symbol,
             "LONG" if direction > 0 else "SHORT", ctx.regime,
             gate.log_line(cand.symbol, sig.strategy), eq.log_line(), outcome)

    return {
        "date": str(trade_date), "symbol": cand.symbol,
        "direction": "LONG" if direction > 0 else "SHORT",
        "driver_strategy": sig.strategy, "signal_time": sig.signal_time,
        "entry_price": ex.entry_fill, "quantity": shares, "position_rs": round(shares * ex.entry_fill, 2),
        "stop_loss": sig.stop, "target": sig.target, "rr": sig.rr, "regime": ctx.regime,
        "ev": round(gate.ev, 4), "disc_ev": round(cand.disc_ev, 4),
        "driver_mu": round(gate.driver_mu, 4),
        "driver_p": round(gate.driver_p, 4), "gate_mult": gate.gate_mult,
        "exec_ms": eq.ms, "exec_ee": eq.ee, "exec_cq": eq.cq, "exec_mq": eq.mq,
        "exec_mult": eq.exec_mult, "context_mult": cm,
        "eff_binary": gate.clusters.eff_binary, "eff_weighted": gate.clusters.eff_weighted,
        "clusters_confirmed": "".join(sorted(gate.clusters.confirmed)),
        "clusters_contradicting": "".join(sorted(gate.clusters.contradicting)),
        "cf_contra": gate.cf_contra, "sector": sector or "",
        "stock_type": stock_type.label(cand.symbol),
        "day_type": tags.day_type, "gap_pct": tags.gap_pct, "breadth": tags.breadth,
        "sector_rs": tags.sector_rs, "daily_trend": tags.daily_trend, "time_bucket": tags.time_bucket,
        "signal_label": label,
        "intended_risk": r.intended_risk, "actual_risk": round(actual_risk, 2),
        "risk_pct": round(actual_risk / CAPITAL * 100, 4),
        "exit_time": ex.exit_time, "exit_price": ex.exit_price, "exit_reason": ex.exit_reason,
        "mfe_r": ex.mfe_r, "mae_r": ex.mae_r, "bars_to_exit": ex.bars_to_exit,
        "settings_hash": ex.settings_hash, "pnl_rs": round(pnl, 2),
        "sizer_flags": ",".join(r.flags),
    }


def _limit_universe(all_data: dict, max_stocks: int) -> dict:
    """Keep the top-N stocks by full-year turnover (for fast bounded runs)."""
    if not max_stocks or len(all_data) <= max_stocks:
        return all_data
    turn = {s: float((df["close"] * df["volume"]).sum()) for s, df in all_data.items()}
    top = sorted(turn, key=turn.get, reverse=True)[:max_stocks]
    return {s: all_data[s] for s in top}


def run_year_bayesian(year: int, bayes: BayesianState | None = None,
                      paper_file=None, save_state=True, days_limit: int | None = None,
                      classifier: RegimeClassifier | None = None,
                      stock_type: StockTypePrior | None = None,
                      decision_bayes: BayesianState | None = None,
                      max_stocks: int | None = None) -> dict:
    """
    Run one year through the Bayesian engine. Posteriors carry across days (and years).
    decision_bayes: WF test-year mode — DECISIONS use this frozen snapshot while the live
    `bayes` keeps updating from test outcomes (never affects current-year decisions).
    """
    bayes = bayes if bayes is not None else BayesianState.load()
    if bayes._cpm is None:
        bayes.attach_changepoint(ChangePointMonitor())     # B3d edge-death detector
    stock_type = stock_type if stock_type is not None else StockTypePrior.load()
    classifier = classifier if classifier is not None else RegimeClassifier()
    paper_file = paper_file or PAPER_TRADES_FILE

    from backtester.regime_data import build_regime_inputs
    regime_inputs = build_regime_inputs(year)              # per-date VIX/ADX/nifty_ret/bands

    all_data, nifty_data = _eng._preload_data(year)
    if max_stocks:
        all_data = _limit_universe(all_data, max_stocks)
    log.info(f"[{year}] {len(all_data)} stocks loaded")
    trading_days = _eng._get_trading_days(all_data, year)
    if days_limit:
        trading_days = trading_days[:days_limit]

    all_recs = []
    n_days = len(trading_days)
    t0 = time.time()
    for i, td in enumerate(trading_days):
        if i == 0 or (i + 1) % 20 == 0 or i + 1 == n_days:
            elapsed = time.time() - t0
            rate = elapsed / (i + 1) if i else 0.0
            eta_min = rate * (n_days - i - 1) / 60
            log.info(f"[{year}] day {i + 1}/{n_days} ({(i + 1) / n_days * 100:.0f}%) "
                     f"| {td} | ETA {eta_min:.1f} min")
        ri = regime_inputs.get(td, {})
        bands = ri.get("vix_bands")
        p80 = bands["p80"] if bands else None
        regime = classifier.classify(ri.get("vix", 15.0), ri.get("adx", 20.0),
                                     ri.get("nifty_ret", 0.0), p80)
        ctx = DayContext(regime=regime, vix=ri.get("vix", 15.0), adx=ri.get("adx", 20.0),
                         bands=bands)
        day = _process_day(td, all_data, nifty_data, bayes, ctx, stock_type,
                           decision_bayes=decision_bayes)
        recs = day.get("recommendations", [])
        all_recs.extend(recs)
        if recs:
            _append_paper(recs, paper_file)

    if save_state:
        bayes.save(BAYES_STATE_FILE)
        stock_type.save()

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
