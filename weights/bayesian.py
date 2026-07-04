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
    BAYES_STATE_FILE, REGIME_MIN_NEFF, K_HIER, STRATEGY_CLUSTERS_FILE,
)

GLOBAL = "global"


def _load_strategy_clusters() -> dict:
    try:
        return json.loads(STRATEGY_CLUSTERS_FILE.read_text())["strategy_to_cluster"]
    except Exception:
        return {}


_S2C = _load_strategy_clusters()   # strategy -> cluster letter (for hierarchical pooling)

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
    mu:              float   # raw posterior mean (mu_raw)
    mu_conservative: float   # 25th-percentile credible bound
    ci_width:        float   # 25–75 interquartile width
    posterior_scale: float   # 1 - ci_width/ci_width_at_prior  (0 uncertain, 1 confident)
    # B3c hierarchical shrinkage (defaults leave Phase-1 behaviour: mu_hier == mu)
    mu_hier:         float | None = None   # strategy shrunk toward its cluster family
    mu_cluster:      float | None = None   # pooled cluster mean (None if unmapped/empty)
    w_hier:          float = 1.0           # n_eff/(n_eff+K_HIER) — own-evidence weight

    @property
    def mu_effective(self) -> float:
        return self.mu if self.mu_hier is None else self.mu_hier

    def p_win(self, shrink_k: float = SHRINK_K) -> float:
        """
        Model-uncertainty-shrunk P(win) (§2f), applied to the hierarchically-shrunk
        mean (B3c order: hierarchical first, then model-uncertainty). Never raw mu.
        """
        w = self.n_eff / (self.n_eff + shrink_k)
        return w * self.mu_effective + (1.0 - w) * PRIOR_PWIN

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


def _blend(p1: Posterior, p2: Posterior, w1: float) -> Posterior:
    """Linear blend of two regime posteriors (B3 Fix 2). mu, ci_width, n_eff blend;
    used only for EV/scoring, so alpha/beta are nominal (never re-sampled downstream)."""
    w1 = max(0.0, min(1.0, w1))
    w2 = 1.0 - w1
    mu = w1 * p1.mu + w2 * p2.mu
    ci = w1 * p1.ci_width + w2 * p2.ci_width
    n_eff = w1 * p1.n_eff + w2 * p2.n_eff
    pscale = max(0.0, min(1.0, 1.0 - ci / _PRIOR_CI_WIDTH))
    return Posterior(
        strategy=p1.strategy, direction=p1.direction,
        alpha=mu * 10.0, beta=(1.0 - mu) * 10.0, n_eff=n_eff,
        mu=mu, mu_conservative=max(0.0, mu - ci / 2.0), ci_width=ci,
        posterior_scale=pscale,
    )


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
        # GLOBAL posterior — {strategy: {direction: _Cell}} (Phase 1 layout, unchanged)
        self._state: dict[str, dict[str, _Cell]] = {}
        # Regime buckets — {strategy: {direction: {regime: _Cell}}} (Phase 2 B3)
        self._rstate: dict[str, dict[str, dict[str, _Cell]]] = {}
        # Pooled cluster posterior — {(cluster, direction): _Cell} (Phase 2 B3c)
        self._pool: dict[tuple, _Cell] = {}

    # ── internal ──────────────────────────────────────────────────────────────
    def _cell(self, strategy: str, direction, regime: str = GLOBAL) -> _Cell:
        d = _dir_key(direction)
        if regime == GLOBAL:
            strat = self._state.setdefault(strategy, {})
            if d not in strat:
                strat[d] = _Cell(self.alpha0, self.beta0, 0.0)
            return strat[d]
        buckets = self._rstate.setdefault(strategy, {}).setdefault(d, {})
        if regime not in buckets:
            buckets[regime] = _Cell(self.alpha0, self.beta0, 0.0)
        return buckets[regime]

    @staticmethod
    def _apply(cell: _Cell, score: float, decay: float) -> None:
        cell.alpha *= decay
        cell.beta  *= decay
        cell.n_eff *= decay
        cell.alpha += score
        cell.beta  += (1.0 - score)
        cell.n_eff += 1.0

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
               risk_amount: float, rr: float, regime: str = GLOBAL) -> dict:
        """
        Apply one settled trade. The GLOBAL posterior is always updated; when a
        non-global regime is given, that regime bucket is updated too (B3). Decay
        is applied to alpha, beta AND n_eff before the new evidence lands.
        """
        score, raw, winsorized = self.score_from_pnl(pnl_rs, risk_amount, rr)

        gcell = self._cell(strategy, direction, GLOBAL)
        self._apply(gcell, score, self.decay)
        if regime and regime != GLOBAL:
            self._apply(self._cell(strategy, direction, regime), score, self.decay)

        # B3c — pooled cluster posterior (decayed sum of member evidence)
        cluster = _S2C.get(strategy)
        if cluster is not None:
            key = (cluster, _dir_key(direction))
            if key not in self._pool:
                self._pool[key] = _Cell(self.alpha0, self.beta0, 0.0)
            self._apply(self._pool[key], score, self.decay)

        if winsorized:
            used_r = max(WINSOR_MAX_LOSS_R, min(rr, raw))
            log.info(f"[OUTLIER_WINSORIZED raw={raw:+.1f}R used={used_r:+.2f}R "
                     f"{strategy} {_dir_key(direction)}]")

        return {
            "strategy": strategy, "direction": _dir_key(direction), "regime": regime,
            "score": round(score, 4), "raw_R": round(raw, 3),
            "winsorized": winsorized,
            "alpha": round(gcell.alpha, 4), "beta": round(gcell.beta, 4),
            "n_eff": round(gcell.n_eff, 4),
        }

    # ── query ─────────────────────────────────────────────────────────────────
    def _posterior_from_cell(self, strategy, direction, cell: _Cell) -> Posterior:
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

    def _cluster_mu(self, strategy: str, direction) -> float | None:
        cluster = _S2C.get(strategy)
        if cluster is None:
            return None
        pool = self._pool.get((cluster, _dir_key(direction)))
        if pool is None:
            return None
        return pool.alpha / (pool.alpha + pool.beta)

    def get_posterior(self, strategy: str, direction, regime: str = GLOBAL) -> Posterior:
        """
        Regime-conditional posterior with fallback (B3) + hierarchical cluster
        shrinkage (B3c). Regime bucket used only when n_eff >= REGIME_MIN_NEFF, else
        global. Then mu_hier = w*mu_raw + (1-w)*mu_cluster, w = n_eff/(n_eff+K_HIER).
        """
        cell = self._cell(strategy, direction, GLOBAL)
        if regime and regime != GLOBAL:
            rcell = self._cell(strategy, direction, regime)
            if rcell.n_eff >= REGIME_MIN_NEFF:
                cell = rcell
        post = self._posterior_from_cell(strategy, direction, cell)

        mu_cluster = self._cluster_mu(strategy, direction)
        if mu_cluster is not None:
            w = cell.n_eff / (cell.n_eff + K_HIER)
            post.w_hier = w
            post.mu_cluster = mu_cluster
            post.mu_hier = w * post.mu + (1.0 - w) * mu_cluster
        else:
            post.mu_hier = post.mu           # unmapped / empty cluster -> no shrinkage
        return post

    def get_posterior_blended(self, strategy: str, direction, vix: float, adx: float,
                              active_regime: str, vix_band_lo: float, vix_band_hi: float,
                              adx_threshold: float, adx_band: float) -> Posterior:
        """
        Boundary blending (B3 Fix 2): linearly blend the two adjacent regime posteriors
        by distance from the boundary. R4 is never blended. Used for EV/scoring only.
        """
        from config.settings import DEFAULT_REGIME
        def P(reg):
            return self.get_posterior(strategy, direction, reg)

        if active_regime == "R4":
            return P("R4")

        # VIX boundary: R1 vs (R2 trending / R3 sideways)
        if vix_band_hi > vix_band_lo and vix_band_lo < vix < vix_band_hi \
                and active_regime in ("R1", "R2", "R3"):
            w_r1 = max(0.0, min(1.0, (vix - vix_band_lo) / (vix_band_hi - vix_band_lo)))
            other = "R2" if adx > adx_threshold else "R3"
            return _blend(P("R1"), P(other), w_r1)

        # ADX boundary: R2 vs R3 (VIX already below band)
        if abs(adx - adx_threshold) < adx_band and active_regime in ("R2", "R3"):
            w_r2 = max(0.0, min(1.0, (adx - (adx_threshold - adx_band)) / (2 * adx_band)))
            return _blend(P("R2"), P("R3"), w_r2)

        return P(active_regime if active_regime else DEFAULT_REGIME)

    def mu(self, strategy: str, direction, regime: str = GLOBAL) -> float:
        return self.get_posterior(strategy, direction, regime).mu

    def has(self, strategy: str, direction) -> bool:
        d = _dir_key(direction)
        return strategy in self._state and d in self._state[strategy]

    # ── persistence ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        """Schema: {strategy: {direction: {"global": cell, "R1": cell, ...}}}."""
        out: dict = {"_meta": {"alpha0": self.alpha0, "beta0": self.beta0, "decay": self.decay},
                     "_pool": {f"{cl}|{d}": asdict(c) for (cl, d), c in sorted(self._pool.items())}}
        strategies = sorted(set(self._state) | set(self._rstate))
        for strat in strategies:
            dirs = {}
            all_dirs = set(self._state.get(strat, {})) | set(self._rstate.get(strat, {}))
            for d in sorted(all_dirs):
                cells = {}
                g = self._state.get(strat, {}).get(d)
                if g is not None:
                    cells[GLOBAL] = asdict(g)
                for reg, c in self._rstate.get(strat, {}).get(d, {}).items():
                    cells[reg] = asdict(c)
                dirs[d] = cells
            out[strat] = dirs
        return out

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
        for key, c in data.get("_pool", {}).items():
            cl, d = key.split("|")
            obj._pool[(cl, d)] = _Cell(float(c["alpha"]), float(c["beta"]), float(c["n_eff"]))
        for strat, dirs in data.items():
            if strat.startswith("_"):
                continue
            for d, val in dirs.items():
                if isinstance(val, dict) and "alpha" in val:      # Phase-1 flat format -> global
                    obj._state.setdefault(strat, {})[d] = _Cell(
                        float(val["alpha"]), float(val["beta"]), float(val["n_eff"]))
                else:                                             # Phase-2 regime-nested format
                    for reg, cell in val.items():
                        c = _Cell(float(cell["alpha"]), float(cell["beta"]), float(cell["n_eff"]))
                        if reg == GLOBAL:
                            obj._state.setdefault(strat, {})[d] = c
                        else:
                            obj._rstate.setdefault(strat, {}).setdefault(d, {})[reg] = c
        return obj
