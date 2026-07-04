"""
B3b — Per-stock behavior prior (plan_phase2.md §2)

One Beta posterior per stock over "does this stock trend or revert?". HDFCBANK
mean-reverts; momentum smallcaps trend. Same strategy, different cluster-weight
modifier per stock.

DECISION-INERT in v1: the modifier is applied ONLY to the informational composite
score (Phase 1 §2h). No gate, EV, ranking, or sizing path reads it — it is a
log-first learner whose evidence becomes a Phase 4b meta-learner feature.

Update (per settled trade, by which cluster drove it):
  A/C (breakout/trend) win  -> trend evidence ; lose -> reversion evidence
  B   (reversion)      win  -> reversion evidence ; lose -> trend evidence
  D/E/F/G: no update (structure/context/meta don't define stock personality)

Modifier at scoring time (× 2 so it is neutral at stock_type_mu = 0.5):
  A/C: stock_type_mu × 2      B: (1 − stock_type_mu) × 2      others: 1.0
  n_eff < STOCK_TYPE_MIN_NEFF -> 1.0 (neutral)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

from config.settings import (
    STOCK_TYPE_ALPHA0, STOCK_TYPE_BETA0, STOCK_TYPE_MIN_NEFF, BAYES_DECAY, STOCK_TYPE_FILE,
)

log = logging.getLogger(__name__)

TREND_CLUSTERS   = {"A", "C"}
REVERT_CLUSTERS  = {"B"}


@dataclass
class _Stock:
    trend_alpha: float
    trend_beta:  float
    n_eff:       float


class StockTypePrior:
    def __init__(self, alpha0: float = STOCK_TYPE_ALPHA0, beta0: float = STOCK_TYPE_BETA0,
                 decay: float = BAYES_DECAY):
        self.alpha0, self.beta0, self.decay = float(alpha0), float(beta0), float(decay)
        self._state: dict[str, _Stock] = {}

    def _cell(self, symbol: str) -> _Stock:
        if symbol not in self._state:
            self._state[symbol] = _Stock(self.alpha0, self.beta0, 0.0)
        return self._state[symbol]

    def update(self, symbol: str, driver_cluster: str, score: float) -> None:
        """score is the [0,1] evidence score of the settled trade (same as BayesianState)."""
        if driver_cluster in TREND_CLUSTERS:
            a_add, b_add = score, 1.0 - score          # trend win -> trend evidence
        elif driver_cluster in REVERT_CLUSTERS:
            a_add, b_add = 1.0 - score, score          # reversion win -> reversion (beta) evidence
        else:
            return                                      # D/E/F/G: no personality update
        c = self._cell(symbol)
        c.trend_alpha *= self.decay
        c.trend_beta  *= self.decay
        c.n_eff       *= self.decay
        c.trend_alpha += a_add
        c.trend_beta  += b_add
        c.n_eff       += 1.0

    def stock_type_mu(self, symbol: str) -> float:
        c = self._state.get(symbol)
        if c is None:
            return 0.5
        return c.trend_alpha / (c.trend_alpha + c.trend_beta)

    def n_eff(self, symbol: str) -> float:
        c = self._state.get(symbol)
        return c.n_eff if c else 0.0

    def get_modifier(self, symbol: str, cluster: str | None) -> float:
        """Cluster-weight multiplier for the composite score. 1.0 when evidence is thin."""
        if cluster not in TREND_CLUSTERS and cluster not in REVERT_CLUSTERS:
            return 1.0
        if self.n_eff(symbol) < STOCK_TYPE_MIN_NEFF:
            return 1.0
        mu = self.stock_type_mu(symbol)
        if cluster in TREND_CLUSTERS:
            return mu * 2.0
        return (1.0 - mu) * 2.0                          # cluster B

    def label(self, symbol: str) -> str:
        mu = self.stock_type_mu(symbol)
        if self.n_eff(symbol) < STOCK_TYPE_MIN_NEFF:
            return f"neutral({mu:.2f})"
        return f"{'trend' if mu >= 0.55 else 'revert' if mu <= 0.45 else 'neutral'}({mu:.2f})"

    # ── persistence ───────────────────────────────────────────────────────────
    def save(self, path: Path | str | None = None) -> Path:
        path = Path(path) if path is not None else STOCK_TYPE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"_meta": {"alpha0": self.alpha0, "beta0": self.beta0, "decay": self.decay},
                   **{s: asdict(c) for s, c in sorted(self._state.items())}}
        path.write_text(json.dumps(payload, indent=2))
        return path

    @classmethod
    def load(cls, path: Path | str | None = None) -> "StockTypePrior":
        path = Path(path) if path is not None else STOCK_TYPE_FILE
        obj = cls()
        if not path.exists():
            return obj
        data = json.loads(path.read_text())
        for s, c in data.items():
            if s.startswith("_"):
                continue
            obj._state[s] = _Stock(float(c["trend_alpha"]), float(c["trend_beta"]), float(c["n_eff"]))
        return obj
