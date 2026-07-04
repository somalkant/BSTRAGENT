"""
B2 — Bayesian cluster gate + EV/driver soft gates  (plan_phase1.md §2f, §2g)

The entry decision. Given a chosen driver signal and all strategies that fired on a
stock-day, decide PASS/REJECT and produce the sizing multiplier gate_mult.

Four gates (all must pass — §2g):
  1. eff_weighted >= 1.5 AND eff_binary >= 1.5   (dependency-penalised, confidence-weighted)
  2. clusters_contradicting <= 1                 (raw; weighted rule logged as [CF_CONTRA])
     + breakout-driver trend-opposition rule (driver cluster A -> cluster C may not contradict)
  3. driver_mu >= 0.52  (ramp to 0.58)           (uses shrunk P(win) per §2f)
  4. EV >= 0.15         (ramp to 0.25)           (shrunk P(win))

gate_mult = ev_mult * driver_mult  (0 => rejected).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import numpy as np

from config.settings import (
    STRATEGY_CLUSTERS_FILE, CLUSTER_CORR_FILE,
    EV_GATE_LOW, EV_GATE_HIGH, DRIVER_MU_LOW, DRIVER_MU_HIGH,
    EFF_CLUSTER_MIN, MAX_CONTRADICTING, VOTE_C_FLOOR, VOTE_C_SCALE,
    CONTEXT_META_CLUSTERS, EVENT_MIN_EV, EVENT_MIN_CLUSTERS,
)
from weights.bayesian import BayesianState

log = logging.getLogger(__name__)


# ── cluster config (module-level cache; reloadable for WF re-estimation) ──────
def _load_cluster_config():
    s2c = json.loads(STRATEGY_CLUSTERS_FILE.read_text())["strategy_to_cluster"]
    cc = json.loads(CLUSTER_CORR_FILE.read_text())
    order = cc["clusters"]
    C = np.array(cc["matrix"], dtype="float64")
    idx = {cl: i for i, cl in enumerate(order)}
    return s2c, order, C, idx


_S2C, _CLUSTER_ORDER, _C, _CIDX = _load_cluster_config()


def reload_cluster_config() -> None:
    global _S2C, _CLUSTER_ORDER, _C, _CIDX
    _S2C, _CLUSTER_ORDER, _C, _CIDX = _load_cluster_config()


def cluster_of(strategy: str) -> str | None:
    return _S2C.get(strategy)


def _eff(vote: dict[str, float]) -> float:
    """Effective independent-evidence count: (Σv)² / (vᵀ C v) over cluster votes."""
    v = np.zeros(len(_CLUSTER_ORDER))
    for cl, val in vote.items():
        if cl in _CIDX:
            v[_CIDX[cl]] = val
    denom = float(v @ _C @ v)
    if denom <= 0:
        return 0.0
    return float(v.sum() ** 2 / denom)


# ── results ───────────────────────────────────────────────────────────────────
@dataclass
class ClusterResult:
    confirmed:      set          # cluster letters confirming the driver direction
    contradicting:  set          # cluster letters firing against
    confidence:     dict         # cluster -> c_i (confirming clusters only)
    eff_binary:     float
    eff_weighted:   float
    p_best:         dict = field(default_factory=dict)   # cluster -> strongest confirming P(win)
    p_best_contra:  dict = field(default_factory=dict)   # cluster -> strongest opposing P(win)


@dataclass
class GateResult:
    passed:        bool
    reason:        str
    gate_mult:     float
    ev:            float
    driver_p:      float          # shrunk P(win) of the driver
    driver_mu:     float          # raw posterior mean of the driver
    ev_mult:       float
    driver_mult:   float
    clusters:      ClusterResult
    cf_contra:     bool = False    # would the weighted contradiction rule have admitted it?

    def log_line(self, symbol: str = "", driver: str = "") -> str:
        c = self.clusters
        conf = "".join(sorted(c.confirmed))
        contra = "".join(sorted(c.contradicting))
        cvec = ",".join(f"{k}:{v:.2f}" for k, v in sorted(c.confidence.items()))
        verdict = ("CONFIRMED—CLEAN" if self.passed and not c.contradicting
                   else "CONFIRMED—CONTESTED" if self.passed
                   else f"REJECTED—{self.reason}")
        return (f"{symbol} {driver} clusters={len(c.confirmed)}({conf}) c=({cvec}) "
                f"eff_w={c.eff_weighted:.2f} eff_bin={c.eff_binary:.2f} vs={len(c.contradicting)}({contra}) "
                f"driver_mu={self.driver_mu:.3f} driver_p={self.driver_p:.3f} EV={self.ev:+.2f} "
                f"gate_mult={self.gate_mult:.2f} [{verdict}]")


# ── cluster counting + confidence votes (§2g) ────────────────────────────────
def count_clusters(signals: dict, driver_direction: int, bayes: BayesianState) -> ClusterResult:
    """
    signals: {strategy_name: Signal}. Returns confirmed/contradicting cluster sets,
    per-confirming-cluster confidence c_i, and eff_binary / eff_weighted.
    """
    confirmed: set = set()
    contradicting: set = set()
    # strongest shrunk P(win) per cluster, confirming and opposing sides
    p_best: dict[str, float] = {}
    p_best_contra: dict[str, float] = {}

    for name, sig in signals.items():
        if sig is None or sig.direction == 0:
            continue
        cl = _S2C.get(name)
        if cl is None:
            continue
        if sig.direction == driver_direction:
            confirmed.add(cl)
            p = bayes.get_posterior(name, driver_direction).p_win()
            if cl not in p_best or p > p_best[cl]:
                p_best[cl] = p
        elif sig.direction == -driver_direction:
            contradicting.add(cl)
            p = bayes.get_posterior(name, -driver_direction).p_win()
            if cl not in p_best_contra or p > p_best_contra[cl]:
                p_best_contra[cl] = p

    # confidence weights c_i (§2g): E/F fixed at 1.0; others from shrunk P_best
    confidence: dict[str, float] = {}
    for cl in confirmed:
        if cl in CONTEXT_META_CLUSTERS:
            confidence[cl] = 1.0
        else:
            p = p_best.get(cl, 0.5)
            confidence[cl] = float(np.clip((p - 0.50) / VOTE_C_SCALE, VOTE_C_FLOOR, 1.0))

    eff_binary = _eff({cl: 1.0 for cl in confirmed}) if confirmed else 0.0
    eff_weighted = _eff(confidence) if confidence else 0.0
    return ClusterResult(confirmed, contradicting, confidence,
                         round(eff_binary, 4), round(eff_weighted, 4), p_best, p_best_contra)


def _ramp(x: float, lo: float, hi: float) -> float:
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


# ── full entry decision ───────────────────────────────────────────────────────
def evaluate_entry(driver_signal, signals: dict, bayes: BayesianState,
                   is_event_day: bool = False) -> GateResult:
    """
    driver_signal: the chosen Signal (has .strategy, .direction, .rr).
    Returns a GateResult with gate_mult (0 => rejected).
    """
    direction = driver_signal.direction
    rr = driver_signal.rr
    post = bayes.get_posterior(driver_signal.strategy, direction)
    driver_p = post.p_win()
    driver_mu = post.mu
    ev = post.ev(rr)

    clusters = count_clusters(signals, direction, bayes)

    # event-day raised bars (§ macro filter RAISE_THRESHOLD mode)
    ev_floor = EVENT_MIN_EV if is_event_day else EV_GATE_LOW
    min_confirmed = EVENT_MIN_CLUSTERS if is_event_day else 2

    def _reject(reason: str, cf: bool = False) -> GateResult:
        return GateResult(False, reason, 0.0, ev, driver_p, driver_mu, 0.0, 0.0, clusters, cf)

    # Breakout-driver trend-opposition rule — hard reject regardless of other gates (§2g)
    driver_cluster = _S2C.get(driver_signal.strategy)
    if driver_cluster == "A" and "C" in clusters.contradicting:
        return _reject("breakout-blocked-by-trend")

    # Gate 1 — dependency-penalised dual cluster gate
    if len(clusters.confirmed) < min_confirmed:
        return _reject("single-cluster" if len(clusters.confirmed) < 2 else "event-day-clusters")
    if clusters.eff_binary < EFF_CLUSTER_MIN or clusters.eff_weighted < EFF_CLUSTER_MIN:
        return _reject("low-effective-clusters")

    # Gate 2 — contradiction (raw). Would the weighted rule (Σ c_j <= 1.0) have
    # admitted a >MAX rejection? -> log [CF_CONTRA] (v1 keeps the raw rule, §2g).
    cf_contra = False
    if len(clusters.contradicting) > MAX_CONTRADICTING:
        weighted_contra = sum(
            float(np.clip((clusters.p_best_contra.get(cl, 0.5) - 0.50) / VOTE_C_SCALE,
                          VOTE_C_FLOOR, 1.0))
            for cl in clusters.contradicting
        )
        cf_contra = weighted_contra <= 1.0
        return _reject("equal-opposition", cf=cf_contra)

    # Gate 3 — driver confidence (shrunk P(win)) soft ramp
    driver_mult = _ramp(driver_p, DRIVER_MU_LOW, DRIVER_MU_HIGH)
    if driver_p < DRIVER_MU_LOW:
        return _reject("weak-driver")

    # Gate 4 — EV soft ramp (shrunk P(win))
    ev_mult = _ramp(ev, ev_floor, EV_GATE_HIGH)
    if ev < ev_floor:
        return _reject("low-EV")

    gate_mult = ev_mult * driver_mult
    if gate_mult <= 0:
        return _reject("zero-gate-mult")

    return GateResult(True, "ok", round(gate_mult, 4), ev, driver_p, driver_mu,
                      round(ev_mult, 4), round(driver_mult, 4), clusters, cf_contra)
