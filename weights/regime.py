"""Regime-based weight modifiers applied before composite scoring.

Phase 2B adds get_direction_bias() — a separate direction-level tilt
applied when comparing the best long candidate vs the best short candidate.

Phase 2 (plan_phase2.md §1) adds the Bayesian regime classifier below: a rolling-
percentile VIX threshold (deterministic, lookahead-free) + hysteresis + R4 crash rule.
The get_regime_modifiers/get_direction_bias functions belong to the deprecated
fixed-weight engine and are kept only for that engine's imports.
"""
from __future__ import annotations

import numpy as np

from config.settings import (
    VIX_PCTILE_WINDOW, VIX_R1_PCTILE, VIX_BAND_LO_PCTILE, VIX_BAND_HI_PCTILE,
    ADX_THRESHOLD, CRASH_NIFTY_RET, HYSTERESIS_DAYS, DEFAULT_REGIME,
)
from config.settings import (HIGH_VIX_THRESHOLD, HIGH_ADX_THRESHOLD,
                              BREAKOUT_REGIME_MULT, REVERSION_REGIME_MULT,
                              BREAKOUT_STRATEGIES, REVERSION_STRATEGIES,
                              SHORT_REGIME_VIX_MULT, LONG_REGIME_BULLISH_MULT,
                              SHORT_REGIME_BEARISH_MULT,
                              NIFTY_BULLISH_THRESHOLD, NIFTY_BEARISH_THRESHOLD)


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 B3 — Bayesian regime classifier
# ═════════════════════════════════════════════════════════════════════════════

def vix_thresholds(trailing_vix_closes) -> dict | None:
    """
    Rolling-percentile VIX thresholds from trailing closes (data <= t-1 ONLY —
    the caller must not include today's VIX). Returns P75/P80/P85 band values,
    or None when there is too little history (falls back to no R1).
    """
    if trailing_vix_closes is None or len(trailing_vix_closes) < 30:
        return None
    arr = np.asarray(list(trailing_vix_closes)[-VIX_PCTILE_WINDOW:], dtype="float64")
    return {
        "p_lo": float(np.percentile(arr, VIX_BAND_LO_PCTILE)),
        "p80":  float(np.percentile(arr, VIX_R1_PCTILE)),
        "p_hi": float(np.percentile(arr, VIX_BAND_HI_PCTILE)),
    }


def raw_regime(vix: float, adx: float, nifty_ret: float, vix_p80: float | None) -> str:
    """Pure threshold logic (no hysteresis). R4 is absolute; R1 uses the rolling P80."""
    if nifty_ret is not None and nifty_ret < CRASH_NIFTY_RET:
        return "R4"
    if vix_p80 is not None and vix > vix_p80:
        return "R1"
    if adx > ADX_THRESHOLD:
        return "R2"
    return "R3"


class RegimeClassifier:
    """
    Stateful daily classifier with a hysteresis buffer: R1/R2/R3 changes commit only
    after HYSTERESIS_DAYS consecutive days; R4 (crash) commits immediately.
    """

    def __init__(self, active: str = DEFAULT_REGIME):
        self.active = active
        self.pending: str | None = None
        self.pending_count = 0

    def classify(self, vix: float, adx: float, nifty_ret: float,
                 vix_p80: float | None) -> str:
        cand = raw_regime(vix, adx, nifty_ret, vix_p80)

        if cand == "R4":                       # crash: immediate, no hysteresis
            self.active, self.pending, self.pending_count = "R4", None, 0
            return "R4"

        if cand == self.active:                # confirms current regime
            self.pending, self.pending_count = None, 0
            return self.active

        if cand == self.pending:               # building toward a change
            self.pending_count += 1
        else:
            self.pending, self.pending_count = cand, 1

        if self.pending_count >= HYSTERESIS_DAYS:
            self.active, self.pending, self.pending_count = cand, None, 0
        return self.active                     # still the old regime until buffer fills

    @property
    def pending_label(self) -> str:
        if self.pending:
            return f"{self.active}[pending->{self.pending} day{self.pending_count}/{HYSTERESIS_DAYS}]"
        return self.active


def get_regime_modifiers(weights: dict, vix: float = 15.0, adx: float = 20.0) -> dict:
    """
    Returns per-strategy multipliers based on current market regime.
    Applied daily — multiplied on top of adaptive weights in composite scorer.
    """
    modifiers = {name: 1.0 for name in weights}

    if vix > HIGH_VIX_THRESHOLD:
        for name in BREAKOUT_STRATEGIES:
            if name in modifiers:
                modifiers[name] = BREAKOUT_REGIME_MULT

    elif adx > HIGH_ADX_THRESHOLD and vix < HIGH_VIX_THRESHOLD:
        for name in REVERSION_STRATEGIES:
            if name in modifiers:
                modifiers[name] = REVERSION_REGIME_MULT

    return modifiers


def get_direction_bias(vix: float = 15.0, nifty_pct_change: float = 0.0) -> tuple[float, float]:
    """
    Returns (long_mult, short_mult) direction-level bias applied when
    comparing the best long candidate vs the best short candidate.

    Rules (applied in order — non-exclusive):
      VIX > 20          → short_mult × SHORT_REGIME_VIX_MULT (volatile market favours shorts)
      Nifty > +1.5%     → long_mult  × LONG_REGIME_BULLISH_MULT  (tilt longs on green day)
      Nifty < -1.5%     → short_mult × SHORT_REGIME_BEARISH_MULT (tilt shorts on red day)
      Otherwise         → no bias (1.0, 1.0)

    Biases can stack: e.g. VIX > 20 AND Nifty < -1.5% → short_mult = 1.3 × 1.2 = 1.56.
    A truly exceptional long signal (score >> threshold) can still win on a red day.
    """
    long_mult  = 1.0
    short_mult = 1.0

    if vix > HIGH_VIX_THRESHOLD:
        short_mult *= SHORT_REGIME_VIX_MULT

    if nifty_pct_change > NIFTY_BULLISH_THRESHOLD:
        long_mult  *= LONG_REGIME_BULLISH_MULT

    if nifty_pct_change < NIFTY_BEARISH_THRESHOLD:
        short_mult *= SHORT_REGIME_BEARISH_MULT

    return long_mult, short_mult
