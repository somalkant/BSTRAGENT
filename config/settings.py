"""
Central configuration — all constants for the trading agent.
Import this everywhere: from config.settings import *
"""

from datetime import time

# ─────────────────────────────────────────────
# PHASE DEFINITIONS
# ─────────────────────────────────────────────
LEARNING_START_YEAR = 2016
LEARNING_END_YEAR   = 2026   # WF6 training: 2023-2026 added to learning set

TESTING_START_YEAR  = 2023
TESTING_END_YEAR    = 2026   # YTD

ALL_YEARS = list(range(LEARNING_START_YEAR, TESTING_END_YEAR + 1))

def get_phase(year: int) -> str:
    if year <= LEARNING_END_YEAR:
        return "LEARNING"
    return "TESTING"

# ─────────────────────────────────────────────
# DATA PARAMETERS
# ─────────────────────────────────────────────
DATA_INTERVAL     = "5minute"
CHUNK_DAYS        = 95          # max days per Kite API call for 5-min (safe margin below 100)
RATE_LIMIT_SLEEP  = 0.38        # seconds between API calls (safe below 3 req/sec)
MAX_RETRIES       = 3
RETRY_BACKOFF     = [5, 15, 60] # seconds to wait on retry 1, 2, 3

MARKET_OPEN  = time(9, 15)      # NSE market open
MARKET_CLOSE = time(15, 30)     # NSE market close
CANDLES_PER_DAY  = 75           # (9:15 to 15:30) / 5 min = 75 candles

# ─────────────────────────────────────────────
# CAPITAL & RISK PARAMETERS
# ─────────────────────────────────────────────
CAPITAL               =  10_00_000   # Rs 10,00,000 (10 Lakhs)
MAX_LOSS_PER_TRADE    =    20_000    # Rs 20,000  (2% of capital)
MAX_CONCURRENT_POSITIONS = 1        # Phase 2: 1 trade/day
MAX_POSITION_SIZE     =   5_00_000  # Rs 5,00,000 (50% of capital — conservative single position)

DAILY_LOSS_LIMIT      =   40_000    # Rs 40,000  — pause all recommendations today
MONTHLY_LOSS_LIMIT    =  1_00_000   # Rs 1,00,000 — pause system, flag for review

NO_ENTRY_AFTER        = time(14, 0)  # 2:00 PM IST — no new positions after this
SQUARE_OFF_TARGET     = time(15, 15) # 3:15 PM IST — close all positions

# ─────────────────────────────────────────────
# TRANSACTION COST MODEL
# ─────────────────────────────────────────────
BROKERAGE_PER_LEG  = 20          # Rs 20 per order (Rs 40 round trip)
STT_RATE_SELL      = 0.00025     # 0.025% on sell side (intraday)
EXCHANGE_RATE      = 0.0000345   # 0.00345% per side
SEBI_RATE          = 0.000001    # 0.0001% per side
GST_RATE           = 0.18        # 18% on brokerage + exchange charges
STAMP_RATE_BUY     = 0.00003     # 0.003% on buy side
SLIPPAGE_PER_SIDE  = 0.0005      # 0.05% assumed slippage each side

def calculate_total_cost(buy_value: float, sell_value: float) -> float:
    brokerage = BROKERAGE_PER_LEG * 2
    stt       = sell_value * STT_RATE_SELL
    exchange  = (buy_value + sell_value) * EXCHANGE_RATE
    sebi      = (buy_value + sell_value) * SEBI_RATE
    gst       = (brokerage + exchange) * GST_RATE
    stamp     = buy_value * STAMP_RATE_BUY
    slippage  = (buy_value + sell_value) * SLIPPAGE_PER_SIDE
    return brokerage + stt + exchange + sebi + gst + stamp + slippage

BREAKEVEN_PCT = 0.0015  # ~0.15% move needed to cover all costs

# ─────────────────────────────────────────────
# QUALITY FILTERS (all 6 must pass to recommend)
# ─────────────────────────────────────────────
LIQUIDITY_MIN_TURNOVER   = 50_00_00_000  # Rs 50 Crore median 20-day daily turnover (Rs 5L position < 1% of daily vol)
MIN_RISK_REWARD          = 1.5    # floor — bad RR<=2 drivers are blocked via DRIVER_BLOCKED, not global threshold
MIN_STRATEGIES_AGREEING  = 4     # raised from 2 — 2-agreement win rate was 32.3% in 2025 (Finding 5)
VOLUME_MULTIPLIER        = 1.5          # current vol > 1.5x same-time-yesterday
MAX_RECOMMENDATIONS      = 3

# Position sizing formula: min(MAX_POSITION_SIZE, MAX_LOSS_PER_TRADE / stop_pct)
def calculate_position_size(stop_loss_pct: float) -> float:
    risk_based = MAX_LOSS_PER_TRADE / stop_loss_pct
    return min(MAX_POSITION_SIZE, risk_based)

# ─────────────────────────────────────────────
# ADAPTIVE WEIGHT SYSTEM
# ─────────────────────────────────────────────
INITIAL_WEIGHT       = 1.0
MIN_WEIGHT           = 0.1    # floor — strategy never fully removed
MAX_WEIGHT           = 3.0    # cap — no single strategy dominates
WEIGHT_UPDATE_EVERY  = 20     # recalculate every N trading days
WEIGHT_SIGNAL_WINDOW = 20     # based on last N signals per strategy

WEIGHT_MULTIPLIERS = {
    "boost":    1.5,   # win_rate > 60%
    "hold":     1.0,   # win_rate 40-60%
    "reduce":   0.5,   # win_rate 30-40%
    "suppress": 0.1,   # win_rate < 30%
}

# Minimum trades in the signal window before a weight update is applied.
# Guards against feedback-loop suppression: a strategy that rarely wins selection
# accumulates sparse data, which can cause premature weight cuts that further
# reduce selection frequency — a self-fulfilling spiral.
MIN_TRADES_FOR_WEIGHT_UPDATE = 10

REVIVAL_WEIGHT = 0.5   # weight reset when suppressed strategy's regime returns

# Win rate thresholds for weight update
WIN_RATE_BOOST    = 0.60
WIN_RATE_HOLD_LOW = 0.40
WIN_RATE_REDUCE   = 0.30

# ─────────────────────────────────────────────
# REGIME OVERRIDES (applied daily before scoring)
# ─────────────────────────────────────────────
HIGH_VIX_THRESHOLD      = 20    # VIX > 20 → suppress breakout strategies
HIGH_ADX_THRESHOLD      = 25    # ADX > 25 with low VIX → suppress reversion strategies

BREAKOUT_REGIME_MULT    = 0.3   # multiplier on breakout weights in high VIX
REVERSION_REGIME_MULT   = 0.5   # multiplier on reversion weights in low VIX + high ADX

# Strategy categories for regime override
BREAKOUT_STRATEGIES  = ["ORB-15", "ORB-30", "PDH-PDL", "GAP-CONT", "VOL-SPIKE",
                         "SR-BREAK", "FIRST-CANDLE", "EMA-CROSS",
                         "ASC-TRI", "BULL-FLAG",
                         # Phase 2B bearish breakdowns also suppressed in low-VIX trending markets
                         "FAILED-BO", "BEAR-FLAG"]
REVERSION_STRATEGIES = ["VWAP-REV", "RSI-EXT", "BOLLINGER", "GAP-FADE",
                         "VWAP-STDDEV", "STOCHASTIC", "CPR", "CAMARILLA",
                         # Phase 2B mean-reversion shorts
                         "DEAD-CAT", "OPEN-WEAK"]
# Reversal pattern strategies (DBL-BTM, FALL-WEDGE, DBL-TOP, PIN-BAR etc.) deliberately
# excluded from regime lists — they perform best during high-volatility regime changes.

# ─────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────
from pathlib import Path

BASE_DIR         = Path(__file__).parent.parent
DATA_DIR         = BASE_DIR / "data"
STOCKS_DIR       = DATA_DIR / "stocks"
INDEX_DIR        = DATA_DIR / "index"
CHECKPOINT_DIR   = BASE_DIR / "checkpoints"
MEMORY_DIR       = BASE_DIR / "memory"
NOTEBOOKS_DIR    = BASE_DIR / "notebooks" / "daily"
REPORTS_DIR      = BASE_DIR / "reports"

PROGRESS_FILE    = CHECKPOINT_DIR / "progress.json"
WEIGHTS_FILE     = CHECKPOINT_DIR / "strategy_weights.json"
UNIVERSE_FILE    = BASE_DIR / "config" / "universe.csv"
TRADE_LOG_DIR    = DATA_DIR / "trade_logs"
PAPER_TRADES_FILE           = TRADE_LOG_DIR / "paper_trades.csv"  # Phase 2 unified log
TESTING_MAX_RECOMMENDATIONS = 1       # Phase 2: 1 trade per day
AGREEMENT_MIN_LIFETIME_WR   = 50.0    # only strategies >= 50% historical win rate count toward agreement

# Conviction-based position sizing — scale risk up when a proven strategy drives the trade
CONVICTION_HIGH_WR   = 65.0  # driver lifetime win% >= 65% → 2x risk (VPOC qualifies; VOL-SPIKE blocked despite 73.8%)
CONVICTION_MED_WR    = 55.0  # driver lifetime win% >= 55% → 1.5x risk
CONVICTION_HIGH_MULT = 2.0   # Rs 10k base → Rs 20k risk
CONVICTION_MED_MULT  = 1.5   # Rs 10k base → Rs 15k risk

# ─────────────────────────────────────────────
# PHASE 2B — SHORT SELLING
# ─────────────────────────────────────────────
SHORT_ENABLED          = True
LOWER_CIRCUIT_BUFFER   = 0.02   # skip short if stock within 2% of lower circuit limit
WEEK52_LOW_BUFFER      = 0.05   # skip short if within 5% of 52-week low on a green Nifty day
NIFTY_GREEN_THRESHOLD  = 0.005  # Nifty up >0.5% is "green" for Filter 9
CORP_EVENT_MOVE_PCT    = 0.05   # skip short if stock moved >5% in prior 3 days (news proxy)

# Direction bias: applied when picking between best long and best short candidate
SHORT_REGIME_VIX_MULT      = 1.3   # VIX > HIGH_VIX_THRESHOLD: short_score × 1.3
LONG_REGIME_BULLISH_MULT   = 1.2   # Nifty > +1.5%: long_score × 1.2
SHORT_REGIME_BEARISH_MULT  = 1.2   # Nifty < -1.5%: short_score × 1.2
NIFTY_BULLISH_THRESHOLD    = 1.5   # % change threshold (positive)
NIFTY_BEARISH_THRESHOLD    = -1.5  # % change threshold (negative)

WF_WEIGHTS_DIR = CHECKPOINT_DIR   # where frozen WF weight snapshots are stored


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1 — BAYESIAN CORE  (plan_phase1.md)
# All constants below replace the fixed-weight system. They are frozen per
# Walk-Forward window and sensitivity-tested in Phase 3. Nothing here is tuned
# mid-run.
# ═════════════════════════════════════════════════════════════════════════════

# ── 2b. Prior ────────────────────────────────────────────────────────────────
BAYES_ALPHA0 = 3.0            # Beta(3,3): posterior mean 0.50 at prior, data
BAYES_BETA0  = 3.0            # dominates after ~60 real outcomes

# ── 2c. Update rule (PnL-normalised, winsorized) ─────────────────────────────
BAYES_DECAY       = 0.999     # applied to alpha, beta, n_eff on every update
WINSOR_MAX_LOSS_R = -1.5      # normalised_pnl floor before scoring (max 1.5R loss)
# upper winsor bound is the trade's own RR (target), applied per-update

# ── 2d. n_eff (effective sample size) ────────────────────────────────────────
# decayed observation count: n_eff = n_eff*DECAY + 1 per update (starts 0.0)

# ── 2e. Bayesian weight (informational composite only, §2h) ──────────────────
# weight = clip((mu_conservative - 0.40) * WEIGHT_SCALE, MIN_WEIGHT, MAX_WEIGHT)
# Anchors: mu_cons 0.50 -> 1.0, 0.40 -> floor. (Plan's 0.65->3.0 anchor is
# approximate under a single linear scale; this term is informational only —
# the decision path uses EV, eff-clusters and Kelly, not this weight.)
BAYES_WEIGHT_SCALE = 10.0
# MIN_WEIGHT (0.1) and MAX_WEIGHT (3.0) reuse the values defined above.

# ── 2f. Model-uncertainty shrinkage ──────────────────────────────────────────
SHRINK_K   = 30.0            # P(win) = w*mu + (1-w)*0.5, w = n_eff/(n_eff+K)
PRIOR_PWIN = 0.50            # neutral prior P(win) shrinkage target

# ── Cold-start burn-in (resolves the clean-start deadlock) ───────────────────
# A clean Beta(3,3) start has mu=0.50 and posterior_scale=0, so the driver gate
# (>=0.52) and Kelly sizing (scale 0 -> size 0) both block the first trade -> no
# trades -> no evidence. The plan's intent ("token size — the posterior still
# collects evidence"; "new strategies: tiny position until evidence accumulates")
# is realised here: while a driver has < BAYES_BURN_IN_NEFF effective trades, the
# evidence gates (driver_mu, EV>=0.15) relax to EV>0 and the trade sizes at a fixed
# tiny exploration risk. Once evidence accrues the strict gates + real Kelly take over.
BAYES_BURN_IN_NEFF    = 20
BURN_IN_RISK_FRACTION = 0.0005    # 0.05% of capital per burn-in exploration trade

# ── 2f/2g. Soft entry gates (ramps, not cliffs) ──────────────────────────────
EV_GATE_LOW   = 0.15         # EV < 0.15 -> reject; ev_mult ramps 0->1 over
EV_GATE_HIGH  = 0.25         #   [0.15, 0.25]
DRIVER_MU_LOW  = 0.52        # driver_mu < 0.52 -> reject; driver_mult ramps
DRIVER_MU_HIGH = 0.58        #   0->1 over [0.52, 0.58]

# ── 2g. Cluster confirmation gate ────────────────────────────────────────────
EFF_CLUSTER_MIN     = 1.5    # require eff_weighted >= 1.5 AND eff_binary >= 1.5
MAX_CONTRADICTING   = 1      # raw contradicting clusters allowed (weighted rule
                             #   logged as [CF_CONTRA] only in v1)
VOTE_C_FLOOR        = 0.3    # confidence-weight floor per confirming cluster
VOTE_C_SCALE        = 0.15   # c_i = clip((P_best-0.50)/SCALE, FLOOR, 1.0)
CONTEXT_META_CLUSTERS = ("E", "F")  # clusters whose vote c_i is fixed at 1.0

# ── 2i. Position sizing — capped fractional Kelly ────────────────────────────
KELLY_FRACTION      = 0.10       # never above 0.15
MAX_RISK_PER_TRADE  = 0.005      # 0.5% of capital (fraction)
MAX_DAILY_RISK      = 0.008      # 0.8% of capital summed across the day
MIS_LEVERAGE        = 5.0        # intraday buying power = cash x leverage
MAX_STOCK_NOTIONAL  = 1.0        # 100% of cash per position (= 20% of 5x BP)
SEBI_MARGIN_FLOOR   = 0.20       # peak-margin floor: margin_rate = max(VaR+ELM, 20%)
LIQUIDITY_ADV_CAP   = 0.01       # position notional <= 1% of 20-day avg turnover
ROUND_RISK_TOLERANCE = 0.25      # skip if |actual-intended risk|/intended > 25%

# ── 2i. Locked-stack loss halts (Phase 3 B5 owns approval; anchors pinned here)
DAILY_HALT_MULT   = 1.2          # daily halt = 1.2x MAX_DAILY_RISK by construction

# ── B2. Execution realism (baseline; full simulator in Phase 3 B5b) ──────────
FILL_MODEL          = "NEXT_BAR_OPEN"   # never signal-candle close (look-ahead)
SLIPPAGE_BPS        = 5.0        # base slippage each side (bps)
SLIPPAGE_IMPACT_K   = 1.0        # impact term coefficient x participation rate

# ── B2. Time filters ─────────────────────────────────────────────────────────
LAST_ENTRY_TIME     = time(14, 30)   # no NEW entries at/after 14:30
EOD_SQUAREOFF_TIME  = time(15, 10)   # frozen EOD square-off (exits run to close)
FIRST_CANDLE_TIME   = "09:15"
FIRST_CANDLE_EXEMPT = {
    "GAP-CONT", "GAP-FADE", "FIRST-CANDLE",
    "PDH-PDL", "PWH-PWL", "CPR", "CAMARILLA",
}

# ── B2. Macro event calendar filter ──────────────────────────────────────────
MACRO_EVENTS      = ["RBI_MPC", "UNION_BUDGET", "US_FED_DECISION"]
EVENT_DAY_MODE    = "SKIP"       # "SKIP" or "RAISE_THRESHOLD"
EVENT_MIN_EV       = 0.35        # RAISE_THRESHOLD mode: EV bar
EVENT_MIN_CLUSTERS = 3           # RAISE_THRESHOLD mode: confirmed-cluster bar

# ── B0/B2. Config artifacts (point-in-time; date-keyed JSON) ─────────────────
STRATEGY_CLUSTERS_FILE = BASE_DIR / "config" / "strategy_clusters.json"
CLUSTER_CORR_FILE      = BASE_DIR / "config" / "cluster_corr.json"
FNO_MEMBERSHIP_FILE    = BASE_DIR / "config" / "fno_membership.json"
NIFTY500_MEMBERSHIP_FILE = BASE_DIR / "config" / "nifty500_membership.json"
ASM_GSM_HISTORY_FILE   = BASE_DIR / "config" / "asm_gsm_history.json"
SECTOR_MAP_FILE        = BASE_DIR / "config" / "sector_map.json"
CORPORATE_ACTIONS_FILE = BASE_DIR / "config" / "corporate_actions.json"
EVENTS_CALENDAR_FILE   = BASE_DIR / "config" / "events_calendar.json"

# ── B1. Bayesian state checkpoint ────────────────────────────────────────────
BAYES_STATE_FILE = CHECKPOINT_DIR / "strategy_bayes.json"

# Initial training window (before first WF freeze) — data <= 2018 only for B0
WF1_TRAIN_END_YEAR = 2018


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 — ENRICHMENT LAYER  (plan_phase2.md)
# ═════════════════════════════════════════════════════════════════════════════

# ── B3. Regime-conditional posteriors ────────────────────────────────────────
REGIME_MIN_NEFF   = 20        # use regime-specific posterior only when n_eff >= 20
REGIMES           = ("R1", "R2", "R3", "R4")   # R1 HIGH_VIX, R2 TRENDING, R3 SIDEWAYS, R4 CRASH
DEFAULT_REGIME    = "R3"      # startup default (SIDEWAYS)

# Rolling-percentile VIX threshold (deterministic, lookahead-free: data <= t-1 only)
VIX_PCTILE_WINDOW = 252       # trailing trading days
VIX_R1_PCTILE     = 80        # regime threshold = trailing P80 of VIX closes
VIX_BAND_LO_PCTILE = 75       # blend zone floor
VIX_BAND_HI_PCTILE = 85       # blend zone ceiling
ADX_THRESHOLD     = 25.0
ADX_BAND          = 2.0       # blend zone ADX in [23, 27]; ADX stays absolute
CRASH_NIFTY_RET   = -2.0      # R4: Nifty day-return < -2%  (absolute)
HYSTERESIS_DAYS   = 3         # R1/R2/R3 change commits after 3 consecutive days; R4 immediate

# ── B3b. Per-stock behavior prior ────────────────────────────────────────────
STOCK_TYPE_ALPHA0 = 5.0       # Beta(5,5): neutral 50/50, more conservative than strategy prior
STOCK_TYPE_BETA0  = 5.0
STOCK_TYPE_MIN_NEFF = 15      # neutral modifier (1.0) below this
STOCK_TYPE_FILE   = CHECKPOINT_DIR / "stock_type_bayes.json"

# ── B3c. Hierarchical cluster priors ─────────────────────────────────────────
K_HIER = 25.0                 # mu_used = w*mu_strategy + (1-w)*mu_cluster, w = n_eff/(n_eff+K_HIER)

# ── B3d. Change-point detection ──────────────────────────────────────────────
CHANGEPOINT_HAZARD = 1.0 / 200.0   # prior prob of a change per trade
CHANGEPOINT_ALARM  = 0.70          # temper posterior when p_change > this
CHANGEPOINT_TEMPER = 0.5           # evidence halved toward prior on alarm
CHANGEPOINT_STATE_FILE = CHECKPOINT_DIR / "changepoint_state.json"

# ── B4e. Execution-quality layer ─────────────────────────────────────────────
EXEC_CHASE_VETO_ATR   = 4.0    # ext > 4 ATR -> hard veto [EXEC_VETO chase]
EXEC_SKIP_FLOOR       = 0.25   # exec_mult < floor -> [EXEC_SKIP]
EXEC_MS_ACCEPT_ATR    = 0.3    # one close beyond a level by > 0.3 ATR accepts it
EXEC_MS_ACCEPT_CLOSES = 2      # or >= 2 consecutive closes beyond
EXEC_EE_EXT_HI        = 4.0    # ee_ext reaches 0.25 at 4 ATR extension
EXEC_EE_VWAP_LO_ATR   = 4.0    # ee_vwap 1.0 within 4 ATR of VWAP
EXEC_EE_VWAP_HI_ATR   = 8.0    # ee_vwap 0.5 at 8 ATR
EXEC_EE_CONSEC_LO     = 4      # ee_consec 1.0 up to 4 same-dir candles
EXEC_EE_CONSEC_HI     = 8      # ee_consec 0.5 at 8

# ── B4f. Context layer ───────────────────────────────────────────────────────
BREADTH_LONG_MIN   = 0.30      # LONG with advancers < 30% -> context_mult 0.7
BREADTH_SHORT_MAX  = 0.70      # SHORT with advancers > 70% -> context_mult 0.7
CONTEXT_MULT_OPPOSED = 0.7
SIGNAL_LABEL_ATR_MULT = 0.5    # signal-outcome label: 0.5 ATR move within
SIGNAL_LABEL_BARS     = 12     #   12 bars (1 hour)
EARNINGS_CALENDAR_FILE = BASE_DIR / "config" / "earnings_calendar.json"

# ── B4c. Meta-strategy external data (optional; graceful fallback) ────────────
PCR_HISTORY_FILE  = BASE_DIR / "config" / "pcr_history.json"    # {date: pcr} or {date:{sym:pcr}}
BLOCK_DEAL_FILE   = BASE_DIR / "config" / "block_deals.json"    # {date: {sym: net_sign}}
