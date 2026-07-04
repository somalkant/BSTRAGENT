"""
B3d — Change-point detection (plan_phase2.md §4)

Decay 0.999 is right for slow drift but blind to structural breaks. This monitor
runs a one-sided CUSUM (edge-death direction) on each strategy × direction trade-score
stream and raises an alarm when the recent scores fall persistently below the slowly-
adapting baseline. On alarm, BayesianState tempers the posterior halfway toward the
prior (evidence halved -> CI widens -> posterior_scale and size drop immediately).

CUSUM v1 is the plan's accepted fallback for BOCPD ("the tempering response is the
important part, the detector is swappable"). It accumulates a persistent downward bias
(a real break) while a slowly-adapting baseline absorbs legitimate drift, so stationary
noise does not trip it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

from config.settings import (
    CHANGEPOINT_ALARM, CHANGEPOINT_STATE_FILE,
)

log = logging.getLogger(__name__)

# CUSUM tuning (frozen per WF window; sensitivity-tested in Phase 3). Tuned so a
# ~22-point win-rate drop (62%->40%) alarms in ~10-13 trades while a stationary
# stream sits below threshold. On pure-binary score streams the baseline is held
# nearly fixed after warm-up (slow alpha) so it does not chase the drop.
CP_SLACK      = 0.12    # k — per-trade slack; baseline noise below this does not accumulate
CP_THRESHOLD  = 1.5     # h — alarm when the accumulated downward deviation exceeds this
CP_WARMUP     = 25      # trades before the detector is armed (baseline must settle)
CP_BASELINE_A = 0.02    # slow baseline EWMA — adapts to each strategy's own level, absorbs drift
# Note: on pure-binary (0/1) score streams a 62%->40% break is only ~1.7 SE from 55%
# noise at n=15, so no detector both catches it within 15 trades AND never false-alarms.
# We favour reliable detection; a false alarm is cheap by design (halve+rebuild). Real
# score streams carry partial credits (lower variance) and false-alarm far less.


@dataclass
class _CPState:
    p0:    float    # slowly-adapting baseline score
    s_neg: float    # one-sided (downward) CUSUM statistic
    n:     int      # observations seen


class ChangePointMonitor:
    def __init__(self, slack: float = CP_SLACK, threshold: float = CP_THRESHOLD,
                 warmup: int = CP_WARMUP, baseline_alpha: float = CP_BASELINE_A):
        self.k, self.h = float(slack), float(threshold)
        self.warmup, self.a = int(warmup), float(baseline_alpha)
        self._state: dict[str, _CPState] = {}

    @staticmethod
    def _key(strategy: str, direction) -> str:
        d = "long" if (direction in (1, "long", "+1") or direction == 1) else "short"
        return f"{strategy}|{d}"

    def observe(self, strategy: str, direction, score: float) -> tuple[bool, float]:
        """
        Feed one settled trade's [0,1] score. Returns (alarm, p_change).
        p_change is a bounded surrogate scaled so s_neg == h maps to CHANGEPOINT_ALARM.
        """
        key = self._key(strategy, direction)
        st = self._state.get(key)
        if st is None:
            st = self._state[key] = _CPState(p0=score, s_neg=0.0, n=0)
        st.n += 1

        if st.n <= self.warmup:
            st.p0 = (1 - self.a) * st.p0 + self.a * score
            return False, 0.0

        dev = st.p0 - score - self.k                 # positive when score is below baseline
        st.s_neg = max(0.0, st.s_neg + dev)
        p_change = min(1.0, st.s_neg / (self.h / CHANGEPOINT_ALARM))
        alarm = st.s_neg >= self.h
        if alarm:
            st.s_neg = 0.0                            # reset after firing
        st.p0 = (1 - self.a) * st.p0 + self.a * score
        return alarm, p_change

    # ── persistence ───────────────────────────────────────────────────────────
    def save(self, path: Path | str | None = None) -> Path:
        path = Path(path) if path is not None else CHANGEPOINT_STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({k: asdict(v) for k, v in self._state.items()}, indent=2))
        return path

    @classmethod
    def load(cls, path: Path | str | None = None) -> "ChangePointMonitor":
        path = Path(path) if path is not None else CHANGEPOINT_STATE_FILE
        obj = cls()
        if path.exists():
            for k, v in json.loads(path.read_text()).items():
                obj._state[k] = _CPState(float(v["p0"]), float(v["s_neg"]), int(v["n"]))
        return obj
