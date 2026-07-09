"""
B2g — Bayesian backtest engine (plan_phase1.md §4 B2)

A clean Bayesian day-by-day engine that replaces the old fixed-weight entry logic.
Reuses the old engine's data helpers; everything decision-related is Bayesian.

Per day:
  0. Macro event gate (SKIP / RAISE_THRESHOLD)
  1. Per stock: data-integrity gate -> generate 37 signals -> first-candle block ->
     last-entry cutoff
  2. Per direction, walk driver-eligible signals (cluster not in E/F, liquidity-eligible
     stocks only -- MIN_ADV_RS) in signal_time order and take the FIRST time-bucket that
     passes the four-gate entry decision (bayesian_gate) -- not the whole day's best-EV
     signal, which would be look-ahead
  3. Across stocks take whichever LONG passes earliest and whichever SHORT passes
     earliest (never the best of the day in hindsight)
  4. Size to the full per-direction daily risk budget (bayesian_sizer) subject to
     liquidity/margin/notional caps, enforce the LONG/SHORT sector rule
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
    STOCKS_DIR, LONG_CAPITAL, SHORT_CAPITAL, DAILY_RISK_CAP_RS,
    PAPER_TRADES_FILE, BAYES_STATE_FILE, DEFAULT_REGIME,
    VIX_BAND_LO_PCTILE, VIX_BAND_HI_PCTILE, ADX_THRESHOLD, ADX_BAND,
)
from strategies import ALL_STRATEGIES
from backtester.bayesian_gate import evaluate_entry, cluster_of, _mins
from backtester.bayesian_sizer import size_trade, sectors_ok
from backtester.execution import simulate_execution
from backtester.filters import first_candle_filter, after_last_entry, event_day, event_mode
from backtester.universe import (
    data_integrity_ok, fno_eligible_short, long_eligible, liquidity_eligible, sector_of,
)
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


def _first_chronological_pass(symbol, signals, bayes, direction, ctx: DayContext, prev, today,
                              get_breadth) -> Candidate | None:
    """First (not highest-EV) driver-eligible strategy in `direction` to pass the gate,
    walking this stock's signals in signal_time order. A live system deciding at the
    moment a signal fires cannot know a better one will show up later in the day, so
    picking the whole day's best (as the old `_pick_driver` did) is look-ahead; this
    stops at the first passing time-bucket and never looks past it. Several strategies
    can share a 5-min timestamp, so ties are evaluated together and the best of THAT
    bucket wins — not just whichever the dict happened to iterate first.
    `bayes` here is the DECISION state (frozen during a WF test year)."""
    is_event = ctx.is_event
    events = []
    for name, sig in signals.items():
        if sig is None or sig.direction != direction or not sig.is_valid:
            continue
        cl = cluster_of(name)
        if cl in DRIVER_INELIGIBLE_CLUSTERS:
            continue
        smin = _mins(sig.signal_time or "09:15")
        if smin is None:
            continue
        events.append((smin, name, sig, cl))
    if not events:
        return None
    events.sort(key=lambda e: e[0])

    i, n = 0, len(events)
    while i < n:
        bucket_min = events[i][0]
        j = i
        while j < n and events[j][0] == bucket_min:
            j += 1
        passers: list[Candidate] = []
        for _, name, sig, cl in events[i:j]:
            gate = evaluate_entry(sig, signals, bayes, is_event_day=is_event, regime=ctx.regime)
            if not gate.passed:
                continue
            # B4e execution quality (L3 Trigger) — veto/skip and exec_mult
            eq = exec_compute(sig, today, prev, cl)
            if eq.veto or eq.skip:
                log.debug(f"{eq.log_line()} {symbol} {name}")
                continue
            cm = context_mult(direction, get_breadth(sig.signal_time or "09:15"))
            disc_ev = gate.ev * eq.exec_mult * cm       # ranking and sizing must agree
            passers.append(Candidate(symbol, sig, gate, signals, eq, disc_ev))
        if passers:
            passers.sort(key=lambda c: (-c.disc_ev, c.driver.strategy))
            return passers[0]
        i = j
    return None


def _earliest(cands: list[Candidate]) -> Candidate | None:
    """Global first-chronological-passer across stocks for one direction: the min, over
    stocks, of each stock's own first passer. Tie-break on identical cross-stock
    timestamps is disc_ev desc then symbol asc -- simultaneous signals are legitimately
    comparable at that instant, which is not lookahead (nothing here depends on signals
    firing after this moment)."""
    if not cands:
        return None
    def key(c: Candidate):
        m = _mins(c.driver.signal_time or "09:15")
        return (m if m is not None else 10**9, -c.disc_ev, c.symbol)
    return min(cands, key=key)


def _trailing_median(history: pd.DataFrame) -> float:
    """history: already filtered to dates < trade_date (see _process_day) — do not
    re-filter the full multi-year frame here, that's the expensive part."""
    if history.empty:
        return 0.0
    daily = history.groupby(history["datetime"].dt.date)["close"].median().tail(5)
    return float(daily.median()) if not daily.empty else 0.0


def _build_breadth_base(today_frames: dict) -> dict:
    """Precompute, once per day, each stock's (open, minute-of-day array, close array) from
    its ALREADY-SLICED per-day frame. This is the fix for the breadth hot-path: previously
    _breadth re-filtered every stock's full multi-year frame (df.datetime.dt.date == date)
    and ran .dt.strftime on every distinct signal-time — O(stocks x full_frame x times/day),
    which exploded on the full universe. Now breadth is a numpy lookup on ~75-row slices."""
    base = {}
    for sym, t in today_frames.items():
        if t is None or len(t) < 1:
            continue
        dt = t["datetime"].dt
        base[sym] = (float(t.iloc[0]["open"]),
                     (dt.hour.to_numpy() * 60 + dt.minute.to_numpy()),
                     t["close"].to_numpy())
    return base


def _breadth(breadth_base: dict, mins_cut: int) -> float | None:
    """Advancer fraction as of mins_cut (minutes since midnight). Lookahead-free."""
    up = tot = 0
    for op, mins, closes in breadth_base.values():
        mask = mins <= mins_cut
        if not mask.any():
            continue
        px = float(closes[mask][-1])
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

    # slice each stock's per-day frame ONCE (reused for signals AND breadth); avoids the
    # full multi-year `.dt.date == trade_date` filter being re-run inside _breadth on
    # every distinct signal-time -- the cause of the recent slowdown at full-universe scale
    today_frames = {sym: _eng._get_today(df, trade_date) for sym, df in all_data.items()}
    breadth_base = _build_breadth_base(today_frames)

    # per-day breadth memo keyed by timestamp (symbol-independent) -> free repeat lookups
    breadth_cache: dict[str, float | None] = {}
    def get_breadth(hhmm: str) -> float | None:
        if hhmm not in breadth_cache:
            m = _mins(hhmm)
            breadth_cache[hhmm] = _breadth(breadth_base, m if m is not None else 555)
        return breadth_cache[hhmm]

    # computed here (not after selection, as before) so liquidity can gate ELIGIBILITY,
    # not just shrink the size of an already-chosen trade -- trailing 20-day, causal
    turnover = _eng._estimate_turnover(all_data, trade_date)

    for symbol, df in all_data.items():
        today = today_frames[symbol]
        if today.empty or len(today) < 10:
            continue
        history = df[df["datetime"].dt.date < trade_date]

        today_med = float(today["close"].median())
        if not data_integrity_ok(today_med, _trailing_median(history), symbol, trade_date):
            continue

        # liquidity gate BEFORE the expensive 53-strategy loop: a stock that can't clear
        # MIN_ADV_RS can never contribute a candidate regardless of direction, so there's
        # no reason to run every strategy against it just to throw the result away. This
        # check only needs turnover (already computed above), not any strategy output.
        adv_rs = turnover.get(symbol, 0.0) * 1e7
        if not liquidity_eligible(adv_rs):
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

        long_d = _first_chronological_pass(symbol, signals, decision_bayes, +1, ctx, prev, today, get_breadth)
        if long_d and long_eligible(symbol, trade_date):
            long_cands.append(long_d)
        short_d = _first_chronological_pass(symbol, signals, decision_bayes, -1, ctx, prev, today, get_breadth)
        if short_d:
            # SHORT_UNIVERSE_COUNTER measures F&O-eligibility impact specifically, among
            # stocks that already clear the liquidity floor (an illiquid stock's short was
            # never tradeable regardless of F&O status, so it shouldn't inflate this metric)
            SHORT_UNIVERSE_COUNTER["gated_shorts"] += 1
            if fno_eligible_short(symbol, trade_date):
                short_cands.append(short_d)
            else:
                SHORT_UNIVERSE_COUNTER["outside_fno"] += 1
                log.debug(f"[SHORT_OUTSIDE_FNO {symbol} {short_d.driver.strategy}]")

    best_long = _earliest(long_cands)
    best_short = _earliest(short_cands)

    recs = []
    # chronological, not disc_ev-first: whichever side's trade was actually knowable
    # first in real time claims sector precedence first (today's disc_ev-first order
    # was the same hindsight artifact in miniature, just bounded to 2 candidates)
    def _order_key(c: Candidate):
        m = _mins(c.driver.signal_time or "09:15")
        return (m if m is not None else 10**9, -c.disc_ev, c.symbol)
    ordered = sorted([c for c in (best_long, best_short) if c], key=_order_key)
    long_sector = short_sector = None
    for cand in ordered:
        # breadth as of this driver's signal time (context_mult input; log tag)
        ctx.breadth = get_breadth(cand.driver.signal_time or "09:15")
        rec = _build_trade(cand, all_data, trade_date, bayes, turnover,
                           long_sector, short_sector, ctx, stock_type, decision_bayes)
        if rec is None:
            continue
        if cand.driver.direction > 0:
            long_sector = rec["sector"]
        else:
            short_sector = rec["sector"]
        recs.append(rec)

    return {"date": str(trade_date), "recommendations": recs}


def _build_trade(cand: Candidate, all_data, trade_date, bayes, turnover,
                 long_sector, short_sector, ctx: DayContext,
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

    cm = context_mult(direction, ctx.breadth)             # B4f: log tag only (not sized on, see sizer)
    adv_rs = turnover.get(cand.symbol, 0.0) * 1e7
    # each direction is its own capital allocation with its own flat daily risk budget
    # (no cross-direction fit_daily_risk needed: at most 1 trade/direction/day, each
    # already sized to exactly its own DAILY_RISK_CAP_RS by size_trade)
    direction_capital = LONG_CAPITAL if direction > 0 else SHORT_CAPITAL
    r = size_trade(sig.entry, sig.stop, sig.rr, gate.ev,
                   capital=direction_capital, adv_turnover_rs=adv_rs,
                   available_cash=direction_capital, burn_in=gate.burn_in)
    if not r.ok:
        cause = " cause=exec_mult" if "LOT_ROUND_SKIP" in r.flags and eq.exec_mult < 1.0 else ""
        log.info(f"[{r.skip_reason.upper()}{cause}] {cand.symbol} {sig.strategy} flags={r.flags}")
        return None
    shares = r.shares

    today = _eng._get_today(all_data[cand.symbol], trade_date)
    ex = simulate_execution(sig, today, shares=shares)
    if not ex.filled:
        return None
    pnl = net_pnl(ex.entry_fill, ex.exit_price, shares, direction=direction)

    # Two DIFFERENT risk bases, deliberately kept separate:
    #  - fill_gap_risk: distance from the ACTUAL fill to the ORIGINAL signal-level stop.
    #    Only meaningful as a one-off safety check for "did the entry itself already gap
    #    into trouble" (e.g. a squeeze release sized on a tiny stop) -- not a stable R-unit.
    #  - realized_risk: the signal's own geometric per-share risk (preserved through
    #    execution.py's re-anchoring, same basis as risk_per_share/mfe_r/mae_r there).
    #    This is what a trade's R-multiple actually means, and what learning/reporting
    #    must use -- using fill_gap_risk here instead would make the Bayesian posteriors
    #    learn from "how far did price happen to drift between signal and fill," which is
    #    noise unrelated to whether the strategy called direction correctly.
    fill_gap_risk = shares * abs(ex.entry_fill - sig.stop)
    realized_risk = shares * abs(sig.entry - sig.stop)

    # Realized-risk cap: the next-bar-open fill can gap far from the signal-level stop,
    # realizing more risk than intended. The hard per-direction daily budget holds on
    # this fill-gap risk too — skip a fill that breaches it (burn-in intends far less
    # than this, so it rarely binds there).
    if fill_gap_risk > DAILY_RISK_CAP_RS + 1e-6:
        log.info(f"[RISK_CAP_SKIP {cand.symbol} {sig.strategy} fill-gap "
                 f"realized=Rs{fill_gap_risk:,.0f}]")
        return None

    # settled trade -> compute the [0,1] evidence score, update driver posterior (with
    # regime tag) and the per-stock behavior prior (B3b), and run change-point detection
    score, _, _ = bayes.score_from_pnl(pnl, realized_risk, sig.rr)
    bayes.update(sig.strategy, direction, pnl_rs=pnl, risk_amount=realized_risk, rr=sig.rr,
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
        "intended_risk": r.intended_risk, "actual_risk": round(realized_risk, 2),
        "risk_pct": round(realized_risk / direction_capital * 100, 4),
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
