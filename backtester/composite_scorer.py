"""
Composite score split for Phase 2B: separate long and short scores.

Weight format (Phase 2B):
    weights[strategy_name] = {"long": float, "short": float}

Backward compat: if a weight value is a plain float (old format), it is
treated as the long weight; short weight defaults to 1.0.
"""
from strategies.base import Signal


def _get_long_weight(weights: dict, name: str) -> float:
    w = weights.get(name, 1.0)
    if isinstance(w, dict):
        return float(w.get("long", 1.0))
    return float(w)


def _get_short_weight(weights: dict, name: str) -> float:
    w = weights.get(name, 1.0)
    if isinstance(w, dict):
        return float(w.get("short", 1.0))
    return 1.0   # old format has no short weight — start neutral


def long_composite_score(
    signals: dict[str, Signal],
    weights: dict,
    regime_modifiers: dict[str, float],
) -> float:
    """Sum of (weight_long × regime_modifier) for all +1 signals."""
    score = 0.0
    for name, sig in signals.items():
        if sig.direction != +1:
            continue
        w   = _get_long_weight(weights, name)
        mod = regime_modifiers.get(name, 1.0)
        score += w * mod
    return round(score, 4)


def short_composite_score(
    signals: dict[str, Signal],
    weights: dict,
    regime_modifiers: dict[str, float],
) -> float:
    """Sum of (weight_short × regime_modifier) for all -1 signals. Always positive."""
    score = 0.0
    for name, sig in signals.items():
        if sig.direction != -1:
            continue
        w   = _get_short_weight(weights, name)
        mod = regime_modifiers.get(name, 1.0)
        score += w * mod
    return round(score, 4)


def composite_score(
    signals: dict[str, Signal],
    weights: dict,
    regime_modifiers: dict[str, float],
) -> float:
    """
    Legacy single-score function — kept for backward compatibility.
    Returns long_score - short_score (net directional score).
    New code should use long_composite_score / short_composite_score directly.
    """
    ls = long_composite_score(signals, weights, regime_modifiers)
    ss = short_composite_score(signals, weights, regime_modifiers)
    return round(ls - ss, 4)


# ─────────────────────────────────────────────────────────────────────────────
# B2b — Bayesian composite score (plan_phase1.md §2h) — INFORMATIONAL ONLY
# Logged, never gated on. Regime modifier is a flat 1.0 in Phase 1 (wired in Phase 2).
# ─────────────────────────────────────────────────────────────────────────────

def _bayesian_score(signals: dict, bayes, direction: int) -> float:
    """Σ max(0, mu_strategy − 0.50) over strategies firing in `direction` (flat 1.0 mult)."""
    score = 0.0
    for name, sig in signals.items():
        if sig is None or sig.direction != direction:
            continue
        mu = bayes.get_posterior(name, direction).mu
        score += max(0.0, mu - 0.50) * 1.0
    return round(score, 4)


def bayesian_long_score(signals: dict, bayes) -> float:
    return _bayesian_score(signals, bayes, +1)


def bayesian_short_score(signals: dict, bayes) -> float:
    return _bayesian_score(signals, bayes, -1)


def bayesian_score_with_stock_type(signals: dict, bayes, direction: int,
                                   symbol: str, stock_type) -> float:
    """
    B3b composite score with the per-stock behavior modifier (plan_phase2.md §2).
    Σ max(0, mu-0.50) × stock_type.get_modifier(symbol, cluster). INFORMATIONAL ONLY —
    decision-inert, exactly like the plain composite score.
    """
    from backtester.bayesian_gate import cluster_of
    score = 0.0
    for name, sig in signals.items():
        if sig is None or sig.direction != direction:
            continue
        mu = bayes.get_posterior(name, direction).mu
        mod = stock_type.get_modifier(symbol, cluster_of(name)) if stock_type else 1.0
        score += max(0.0, mu - 0.50) * mod
    return round(score, 4)


def count_agreeing(signals: dict[str, Signal], direction: int) -> int:
    """Count how many strategies agree on a direction."""
    return sum(1 for s in signals.values() if s.direction == direction)


def count_agreeing_filtered(
    signals: dict[str, Signal],
    direction: int,
    lifetime_wr: dict,
    min_wr: float = 50.0,
) -> int:
    """
    Count strategies that agree on direction AND have lifetime win rate >= min_wr.
    Used in Phase 2 testing — poor strategies are excluded automatically.
    Supports direction-specific win rates: lifetime_wr values may be
    {"long": x, "short": y} dicts or plain floats (backward compat).
    """
    count = 0
    dir_key = "long" if direction == +1 else "short"
    for name, s in signals.items():
        if s.direction != direction:
            continue
        entry = lifetime_wr.get(name)
        if isinstance(entry, dict):
            wr = float(entry.get(dir_key, 50.0))
        elif entry is not None:
            wr = float(entry)
        else:
            wr = 50.0
        if wr >= min_wr:
            count += 1
    return count
