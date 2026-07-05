"""
B4e — Execution Quality Layer (plan_phase2.md §B4e, Level 3 Trigger)

The Phase 1 gates grade the SETUP; this grades the ENTRY at signal time. Three
sizing-active components (ms, ee, cq) and one log-only (mq) multiply into position size.
Exactly ONE hard veto exists: extreme chase (ext > 4 ATR).

    exec_mult = ms x ee x cq        (mq excluded from sizing in v1)
    exec_mult < EXEC_SKIP_FLOOR -> skip [EXEC_SKIP]
    ext > EXEC_CHASE_VETO_ATR   -> veto [EXEC_VETO chase]

All state is computed from data <= the signal bar (determinism is an exit criterion):
fills are next-bar open, so the completed signal bar is known without lookahead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config.settings import (
    EXEC_CHASE_VETO_ATR, EXEC_SKIP_FLOOR, EXEC_MS_ACCEPT_ATR, EXEC_MS_ACCEPT_CLOSES,
    EXEC_EE_EXT_HI, EXEC_EE_VWAP_LO_ATR, EXEC_EE_VWAP_HI_ATR,
    EXEC_EE_CONSEC_LO, EXEC_EE_CONSEC_HI,
)

log = logging.getLogger(__name__)

TREND_LIKE = {"A", "C", "G"}    # breakout/trend/multi-tf — chase & vwap checks apply
ATR_LEN = 14


@dataclass
class ExecQuality:
    ms:        float
    ee:        float
    cq:        float
    mq:        float          # log-only — never multiplied into size
    exec_mult: float
    veto:      bool
    skip:      bool
    reason:    str

    def log_line(self) -> str:
        tag = " [EXEC_VETO chase]" if self.veto else " [EXEC_SKIP]" if self.skip else ""
        return (f"exec: ms={self.ms:.2f} ee={self.ee:.2f} cq={self.cq:.2f} "
                f"(mq={self.mq:.2f} log) -> {self.exec_mult:.2f}{tag}")


def _ramp(x, x0, x1, y0, y1):
    if x1 == x0:
        return y1
    t = (x - x0) / (x1 - x0)
    return float(min(max(y0 + t * (y1 - y0), min(y0, y1)), max(y0, y1)))


def _atr(bars: pd.DataFrame) -> float:
    if len(bars) < 2:
        return 0.0
    h, l, c = bars["high"], bars["low"], bars["close"].shift(1)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return float(tr.tail(ATR_LEN).mean())


def _levels(signal, bars_upto, prev_day) -> list[float]:
    """Structural levels that exist at signal time (menu; opening range only from 09:45)."""
    lv = []
    if prev_day is not None:
        lv += [float(prev_day["high"]), float(prev_day["low"])]
        pivot = (float(prev_day["high"]) + float(prev_day["low"]) + float(prev_day["close"])) / 3
        lv.append(pivot)                                    # CPR pivot
    # session VWAP (participates from 09:35 — needs a few bars)
    if len(bars_upto) >= 4:
        tp = (bars_upto["high"] + bars_upto["low"] + bars_upto["close"]) / 3
        vwap = float((tp * bars_upto["volume"]).sum() / max(bars_upto["volume"].sum(), 1))
        lv.append(vwap)
    # opening range high/low — only exists from 09:45 (>= 6 five-min bars)
    if len(bars_upto) >= 6:
        orb = bars_upto.iloc[:6]
        lv += [float(orb["high"].max()), float(orb["low"].min())]
    return [x for x in lv if x > 0]


def _accepted(level: float, bars_upto: pd.DataFrame, direction: int, atr: float) -> bool:
    """A level is accepted in the trade direction: >=2 consecutive closes beyond it, OR
    one close beyond by > EXEC_MS_ACCEPT_ATR x ATR."""
    closes = bars_upto["close"].tail(6).to_numpy()
    if direction > 0:
        beyond = closes > level
        strong = (closes[-1] - level) > EXEC_MS_ACCEPT_ATR * atr if atr > 0 else False
    else:
        beyond = closes < level
        strong = (level - closes[-1]) > EXEC_MS_ACCEPT_ATR * atr if atr > 0 else False
    consec = beyond[-EXEC_MS_ACCEPT_CLOSES:].all() if len(beyond) >= EXEC_MS_ACCEPT_CLOSES else False
    return bool(consec or strong)


def compute(signal, today_5min: pd.DataFrame, prev_day, driver_cluster: str) -> ExecQuality:
    direction = signal.direction
    entry, target = float(signal.entry), float(signal.target)

    # bars up to and including the signal bar (no lookahead)
    smin = signal.signal_time or "09:15"
    bars = today_5min[today_5min["datetime"].dt.strftime("%H:%M") <= smin].reset_index(drop=True)
    if len(bars) < 2:
        return ExecQuality(1, 1, 1, 1, 1.0, False, False, "insufficient-bars")
    atr = _atr(bars)
    sig_bar = bars.iloc[-1]

    # ── Component 2: entry efficiency / anti-chase (ee) + the one hard veto ──
    look = bars["close"].tail(7).to_numpy()
    ext = abs(entry - look[0]) / atr if atr > 0 else 0.0        # extension over ~6 bars
    if ext > EXEC_CHASE_VETO_ATR:
        return ExecQuality(1, _ramp(ext, 1, EXEC_EE_EXT_HI, 1, 0.25), 1, 0, 0.0, True, False,
                           f"chase ext={ext:.1f}ATR")
    ee_ext = _ramp(ext, 1.0, EXEC_EE_EXT_HI, 1.0, 0.25)
    ee = ee_ext
    if driver_cluster in TREND_LIKE and atr > 0:
        tp = (bars["high"] + bars["low"] + bars["close"]) / 3
        vwap = float((tp * bars["volume"]).sum() / max(bars["volume"].sum(), 1))
        ee_vwap = _ramp(abs(entry - vwap) / atr, EXEC_EE_VWAP_LO_ATR, EXEC_EE_VWAP_HI_ATR, 1.0, 0.5)
        d = np.sign(bars["close"].diff().tail(10).to_numpy())
        run = 0
        for x in d[::-1]:
            if x == np.sign(direction):
                run += 1
            else:
                break
        ee_consec = _ramp(run, EXEC_EE_CONSEC_LO, EXEC_EE_CONSEC_HI, 1.0, 0.5)
        ee = min(ee_ext, ee_vwap, ee_consec)

    # ── Component 1: market-structure acceptance (ms) ──
    ms = 1.0
    levels = _levels(signal, bars, prev_day)
    span = target - entry
    if span != 0:
        opposing = []
        for lv in levels:
            between = (entry < lv < target) if span > 0 else (target < lv < entry)
            if between and not _accepted(lv, bars, direction, atr):
                opposing.append(lv)
        if opposing:
            nearest = min(opposing, key=lambda lv: abs(lv - entry))
            block_frac = (nearest - entry) / span
            if block_frac <= 0.33:
                ms = 0.25
            elif block_frac <= 0.66:
                ms = _ramp(block_frac, 0.33, 0.66, 0.25, 1.0)
            else:
                ms = 1.0
            if driver_cluster == "B":                       # reversion trades AT levels
                ms = max(0.50, ms)

    # ── Component 3: trigger-candle quality (cq) ──
    o, h, l, c = float(sig_bar["open"]), float(sig_bar["high"]), float(sig_bar["low"]), float(sig_bar["close"])
    rng = max(h - l, 1e-9)
    body_frac = abs(c - o) / rng
    close_loc = (c - l) / rng if direction > 0 else (h - c) / rng
    if direction > 0:
        opp_wick = (min(o, c) - l) / max(abs(c - o), 0.1 * rng)
    else:
        opp_wick = (h - max(o, c)) / max(abs(c - o), 0.1 * rng)
    cq = float(np.clip(0.5 * body_frac + 0.5 * close_loc, 0.2, 1.0))
    if opp_wick >= 2 and rng > 1.5 * atr and atr > 0:
        cq = 0.2

    # ── Component 4: momentum quality (mq) — LOG-ONLY ──
    atr5 = _atr(bars.tail(5 + ATR_LEN)) if len(bars) > 5 else atr
    atr20 = _atr(bars.tail(20 + ATR_LEN)) if len(bars) > 20 else atr
    vol_ratio = (bars["volume"].tail(3).mean() / max(bars["volume"].tail(20).mean(), 1))
    rng_exp = rng / atr if atr > 0 else 1.0
    net = abs(float(bars["close"].iloc[-1]) - float(bars["close"].tail(6).iloc[0]))
    path = float(bars["close"].diff().abs().tail(6).sum())
    close_eff = net / path if path > 0 else 0.0
    mq = float(np.mean([np.clip(atr5 / max(atr20, 1e-9), 0, 2) / 2,
                        np.clip(vol_ratio, 0, 3) / 3,
                        np.clip(rng_exp, 0, 3) / 3,
                        np.clip(close_eff, 0, 1)]))

    exec_mult = ms * ee * cq
    skip = exec_mult < EXEC_SKIP_FLOOR
    return ExecQuality(round(ms, 3), round(ee, 3), round(cq, 3), round(mq, 3),
                       round(exec_mult, 3), False, skip, "skip" if skip else "ok")
