from strategies.breakout.orb import ORB15, ORB30
from strategies.breakout.pdh_pdl import PDH_PDL
from strategies.breakout.gap_continuation import GapContinuation
from strategies.breakout.volume_spike import VolumeSpikeBreakout
from strategies.mean_reversion.vwap_reversion import VWAPReversion
from strategies.mean_reversion.rsi_extremes import RSIExtremes
from strategies.mean_reversion.bollinger import BollingerReversion
from strategies.mean_reversion.gap_fade import GapFade
from strategies.trend.ema_crossover import EMACrossover
from strategies.trend.supertrend import Supertrend
from strategies.trend.macd import MACDCrossover
from strategies.price_action.sr_breakout import SRBreakout
from strategies.price_action.nr7_inside import NR7InsideDay
from strategies.price_action.first_candle import FirstCandle
from strategies.pivot.cpr import CPR
from strategies.pivot.camarilla import Camarilla
from strategies.volatility.adx_filter import ADXFilter
from strategies.volatility.vwap_stddev import VWAPStdDev
from strategies.oscillator.stochastic import StochasticCrossover
from strategies.oscillator.volume_profile import VolumeProfile
from strategies.market_relative.relative_strength import RelativeStrength
from strategies.chart_patterns.double_bottom import DoubleBottom
from strategies.chart_patterns.falling_wedge import FallingWedge
from strategies.chart_patterns.ascending_triangle import AscendingTriangle
from strategies.chart_patterns.bull_flag import BullFlag
from strategies.multi_timeframe.daily_trend_bias import DailyTrendBias
# Phase 2B — bearish-only strategies
from strategies.bearish.double_top import DoubleTop
from strategies.bearish.desc_triangle import DescendingTriangle
from strategies.bearish.rise_wedge import RisingWedge
from strategies.bearish.bear_flag import BearFlag
from strategies.bearish.failed_breakout import FailedBreakout
from strategies.bearish.dead_cat import DeadCatBounce
from strategies.bearish.open_weakness import OpenWeakness
from strategies.bearish.bear_engulf import BearEngulfing
# Phase 2B — dual-direction strategies
from strategies.dual.pin_bar import PinBar
from strategies.dual.intraday_struct import IntradayStructure
# Phase 2 — 16 new strategies (B4)
from strategies.breakout.pwh_pwl import PriorWeekBreakout            # A
from strategies.breakout.ttm_squeeze import TTMSqueeze              # A
from strategies.reversion.keltner_rev import KeltnerReversion       # B
from strategies.reversion.mfi_divergence import MFIDivergence       # B
from strategies.reversion.rsi_divergence import RSIDivergence       # B
from strategies.reversion.vol_price_div import VolumePriceDivergence  # B
from strategies.trend.parabolic_sar import ParabolicSAR            # C
from strategies.structure.three_bar_rev import ThreeBarReversal     # D
from strategies.structure.fvg import FairValueGap                   # D
from strategies.context.fib_retracement import FibRetracement       # E
from strategies.meta.options_pcr import OptionsPCR                  # F
from strategies.meta.day_seasonality import DaySeasonality          # F
from strategies.meta.pre_expiry import PreExpiry                    # F
from strategies.meta.block_deal import BlockDeal                    # F
from strategies.multiframe.mtf_orb import MTF15mORB                 # G
from strategies.multiframe.mtf_ema import MTF15mEMA                 # G

ALL_STRATEGIES = [
    ORB15(), ORB30(), PDH_PDL(), GapContinuation(), VolumeSpikeBreakout(),
    VWAPReversion(), RSIExtremes(), BollingerReversion(), GapFade(),
    EMACrossover(), Supertrend(), MACDCrossover(),
    SRBreakout(), NR7InsideDay(), FirstCandle(),
    CPR(), Camarilla(),
    ADXFilter(), VWAPStdDev(),
    StochasticCrossover(), VolumeProfile(),
    RelativeStrength(),
    DoubleBottom(), FallingWedge(), AscendingTriangle(), BullFlag(),
    DailyTrendBias(),
    # Phase 2B
    DoubleTop(), DescendingTriangle(), RisingWedge(), BearFlag(),
    FailedBreakout(), DeadCatBounce(), OpenWeakness(), BearEngulfing(),
    PinBar(), IntradayStructure(),
    # Phase 2 — B4 new strategies
    PriorWeekBreakout(), TTMSqueeze(),
    KeltnerReversion(), MFIDivergence(), RSIDivergence(), VolumePriceDivergence(),
    ParabolicSAR(),
    ThreeBarReversal(), FairValueGap(),
    FibRetracement(),
    OptionsPCR(), DaySeasonality(), PreExpiry(), BlockDeal(),
    MTF15mORB(), MTF15mEMA(),
]

STRATEGY_NAMES = [s.name for s in ALL_STRATEGIES]
