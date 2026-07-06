"""
Config-integrity stamp (plan_phase1.md B2 excursion record / Phase 3 B5).

settings_hash() hashes the frozen Phase 1 decision constants so every trade record can
be stamped and any mid-window drift raises a [CONFIG_DRIFT] flag downstream.
"""
from __future__ import annotations

import hashlib
import json

from config import settings as S

# The frozen constants that define the system's decisions. Order-independent (sorted).
_FROZEN_KEYS = [
    "BAYES_ALPHA0", "BAYES_BETA0", "BAYES_DECAY", "WINSOR_MAX_LOSS_R",
    "SHRINK_K", "PRIOR_PWIN", "BAYES_WEIGHT_SCALE", "MIN_WEIGHT", "MAX_WEIGHT",
    "EV_GATE_LOW", "EV_GATE_HIGH", "DRIVER_MU_LOW", "DRIVER_MU_HIGH",
    "EFF_CLUSTER_MIN", "MAX_CONTRADICTING", "VOTE_C_FLOOR", "VOTE_C_SCALE",
    "LONG_CAPITAL", "SHORT_CAPITAL", "DAILY_RISK_CAP_RS",
    "MAX_RISK_PER_TRADE", "MAX_DAILY_RISK", "MIS_LEVERAGE",
    "MAX_STOCK_NOTIONAL", "SEBI_MARGIN_FLOOR", "LIQUIDITY_ADV_CAP", "ROUND_RISK_TOLERANCE",
    "SLIPPAGE_BPS", "SLIPPAGE_IMPACT_K",
    "EVENT_DAY_MODE", "EVENT_MIN_EV", "EVENT_MIN_CLUSTERS",
]


def frozen_config() -> dict:
    out = {}
    for k in _FROZEN_KEYS:
        v = getattr(S, k, None)
        out[k] = str(v) if not isinstance(v, (int, float, str, bool, type(None))) else v
    # time-valued constants stringified
    out["LAST_ENTRY_TIME"] = str(getattr(S, "LAST_ENTRY_TIME", None))
    out["EOD_SQUAREOFF_TIME"] = str(getattr(S, "EOD_SQUAREOFF_TIME", None))
    return out


def settings_hash() -> str:
    blob = json.dumps(frozen_config(), sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
