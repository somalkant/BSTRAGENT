"""
B1 — Bayesian State Layer  (plan_phase1.md §2a–2f)

Replaces the fixed-weight system (weights/adaptive.py, backtester/winrate_updater.py).

Per strategy × direction we hold a Beta(alpha, beta) posterior over the strategy's
"win quality" plus a decayed effective sample size n_eff. Every settled trade updates
the DRIVER strategy's posterior only (§2c driver-only rule — the engine enforces the
driver-only scope; this class just applies whatever update it is given).

Key formulas
------------
score      = (winsorize(pnl/risk, -1.5, RR) + 1) / (RR + 1)        -> [0, 1]
update     : alpha,beta,n_eff *= DECAY ; alpha += score ; beta += 1-score ; n_eff += 1
mu         = alpha / (alpha + beta)
mu_cons    = Beta.ppf(0.25, alpha, beta)                            (25th pct bound)
P(win)     = w*mu + (1-w)*0.5 ,  w = n_eff/(n_eff+SHRINK_K)         (§2f shrinkage)
EV         = P(win)*(RR+1) - 1
Kelly      = EV / RR
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

from scipy.stats import beta as beta_dist

from config.settings import (
    BAYES_ALPHA0, BAYES_BETA0, BAYES_DECAY, WINSOR_MAX_LOSS_R,
    SHRINK_K, PRIOR_PWIN, BAYES_WEIGHT_SCALE, MIN_WEIGHT, MAX_WEIGHT,
    BAYES_STATE_FILE,
)

log = logging.getLogger(__name__)


def _dir_key(direction) -> str:
    """Accept +1/-1 ints or 'long'/'short' strings; normalise to 'long'/'short'."""
    if isinstance(direction, str):
        d = direction.lower()
        if d in ("long", "short"):
            return d
        raise ValueError(f"bad direction {direction!r}")
    if direction > 0:
        return "long"
    if direction < 0:
        return "short"
    raise ValueError("direction 0 has no posterior")


# CI width of the pristine prior — cached; used by posterior_scale (§2i).
_PRIOR_CI_WIDTH = float(
    beta_dist.ppf(0.75, BAYES_ALPHA0, BAYES_BETA0)
    - beta_dist.ppf(0.25, BAYES_ALPHA0, BAYES_BETA0)
)


@dataclass
class Posterior:
    """Read-only view returned by get_posterior()."""
    strategy:        str
    direction:       str
    alpha:           float
    beta:            float
    n_eff:           float
    mu:              float   # posterior mean
    mu_conservative: float   # 25th-percentile credible bound
    ci_width:        float   # 25–75 interquartile width
    posterior_scale: float   # 1 - ci_width/ci_width_at_prior  (0 uncertain, 1 confident)

    def p_win(self, shrink_k: float = SHRINK_K) -> float:
        """Model-uncertainty-shrunk P(win) (§2f). Always used instead of raw mu."""
        w = self.n_eff / (self.n_eff + shrink_k)
        return w * self.mu + (1.0 - w) * PRIOR_PWIN

    def ev(self, rr: float, shrink_k: float = SHRINK_K) -> float:
        """Bayesian EV on the shrunk P(win): EV = P*(RR+1) - 1 (§2f)."""
        return self.p_win(shrink_k) * (rr + 1.0) - 1.0

    def kelly(self, rr: float, shrink_k: float = SHRINK_K) -> float:
        """Kelly fraction = EV / RR (§2i). Can be negative — caller gates on EV first."""
        if rr <= 0:
            return 0.0
        return self.ev(rr, shrink_k) / rr

    def weight(self) -> float:
        """Informational composite weight (§2e/§2h). Not a decision input in Phase 1."""
        w = (self.mu_conservative - 0.40) * BAYES_WEIGHT_SCALE
        return max(MIN_WEIGHT, min(MAX_WEIGHT, w))


@dataclass
class _Cell:
    alpha: float
    beta:  float
    n_eff: float


class BayesianState:
    """
    Holds {strategy: {'long': _Cell, 'short': _Cell}}. The regime dimension is
    added in Phase 2 — Phase 1 is one flat posterior per strategy × direction.
    """

    def __init__(self, alpha0: float = BAYES_ALPHA0, beta0: float = BAYES_BETA0,
                 decay: float = BAYES_DECAY):
        self.alpha0 = float(alpha0)
        self.beta0  = float(beta0)
        self.decay  = float(decay)
        self._state: dict[str, dict[str, _Cell]] = {}

    # ── internal ──────────────────────────────────────────────────────────────
    def _cell(self, strategy: str, direction) -> _Cell:
        d = _dir_key(direction)
        strat = self._state.setdefault(strategy, {})
        if d not in strat:
            strat[d] = _Cell(self.alpha0, self.beta0, 0.0)
        return strat[d]

    # ── scoring ───────────────────────────────────────────────────────────────
    @staticmethod
    def score_from_pnl(pnl_rs: float, risk_amount: float, rr: float) -> tuple[float, float, bool]:
        """
        Map a settled trade to a [0,1] evidence score (§2c).
        Returns (score, raw_normalised_pnl, winsorized_flag).

        The evidence score is winsorized to [WINSOR_MAX_LOSS_R, RR]; real PnL is
        never winsorized (that stays in the trade log / accounting).
        """
        if risk_amount is None or risk_amount <= 0 or rr <= 0:
            # Degenerate risk — treat as neutral, no evidence movement.
            return 0.5, 0.0, False
        raw = pnl_rs / risk_amount
        clipped = max(WINSOR_MAX_LOSS_R, min(rr, raw))
        winsorized = clipped != raw
        score = (clipped + 1.0) / (rr + 1.0)
        score = max(0.0, min(1.0, score))
        return score, raw, winsorized

    # ── update ────────────────────────────────────────────────────────────────
    def update(self, strategy: str, direction, pnl_rs: float,
               risk_amount: float, rr: float) -> dict:
        """
        Apply one settled trade to the (strategy, direction) posterior.
        Decay is applied to alpha, beta AND n_eff before the new evidence lands.
        Returns a small dict describing the update (for logging / tests).
        """
        score, raw, winsorized = self.score_from_pnl(pnl_rs, risk_amount, rr)
        cell = self._cell(strategy, direction)

        cell.alpha *= self.decay
        cell.beta  *= self.decay
        cell.n_eff *= self.decay

        cell.alpha += score
        cell.beta  += (1.0 - score)
        cell.n_eff += 1.0

        if winsorized:
            used_r = max(WINSOR_MAX_LOSS_R, min(rr, raw))
            log.info(f"[OUTLIER_WINSORIZED raw={raw:+.1f}R used={used_r:+.2f}R "
                     f"{strategy} {_dir_key(direction)}]")

        return {
            "strategy": strategy, "direction": _dir_key(direction),
            "score": round(score, 4), "raw_R": round(raw, 3),
            "winsorized": winsorized,
            "alpha": round(cell.alpha, 4), "beta": round(cell.beta, 4),
            "n_eff": round(cell.n_eff, 4),
        }

    # ── query ─────────────────────────────────────────────────────────────────
    def get_posterior(self, strategy: str, direction) -> Posterior:
        cell = self._cell(strategy, direction)
        a, b = cell.alpha, cell.beta
        mu = a / (a + b)
        q25 = float(beta_dist.ppf(0.25, a, b))
        q75 = float(beta_dist.ppf(0.75, a, b))
        ci_width = q75 - q25
        posterior_scale = max(0.0, min(1.0, 1.0 - ci_width / _PRIOR_CI_WIDTH))
        return Posterior(
            strategy=strategy, direction=_dir_key(direction),
            alpha=a, beta=b, n_eff=cell.n_eff,
            mu=mu, mu_conservative=q25, ci_width=ci_width,
            posterior_scale=posterior_scale,
        )

    def mu(self, strategy: str, direction) -> float:
        return self.get_posterior(strategy, direction).mu

    def has(self, strategy: str, direction) -> bool:
        d = _dir_key(direction)
        return strategy in self._state and d in self._state[strategy]

    # ── persistence ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "_meta": {"alpha0": self.alpha0, "beta0": self.beta0, "decay": self.decay},
            **{
                strat: {d: asdict(cell) for d, cell in dirs.items()}
                for strat, dirs in sorted(self._state.items())
            },
        }

    def save(self, path: Path | str | None = None) -> Path:
        path = Path(path) if path is not None else BAYES_STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def load(cls, path: Path | str | None = None) -> "BayesianState":
        path = Path(path) if path is not None else BAYES_STATE_FILE
        obj = cls()
        if not path.exists():
            return obj
        data = json.loads(path.read_text())
        meta = data.get("_meta", {})
        obj.alpha0 = float(meta.get("alpha0", BAYES_ALPHA0))
        obj.beta0  = float(meta.get("beta0", BAYES_BETA0))
        obj.decay  = float(meta.get("decay", BAYES_DECAY))
        for strat, dirs in data.items():
            if strat.startswith("_"):
                continue
            for d, cell in dirs.items():
                obj._state.setdefault(strat, {})[d] = _Cell(
                    float(cell["alpha"]), float(cell["beta"]), float(cell["n_eff"]))
        return obj
