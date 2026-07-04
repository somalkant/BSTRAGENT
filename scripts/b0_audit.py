"""
B0 (step 2) — Correlation Audit  (plan_phase1.md §4 B0, steps 2-7)

Consumes the firing log from scripts/b0_signal_log.py and produces:
  - pairwise strategy signal-correlation matrix; flags Pearson r > 0.70
  - the inter-cluster correlation matrix C (PSD-guaranteed) -> config/cluster_corr.json
  - an audit report -> reports/b0_audit.md

This is a signal-correlation scan only — no trades, no P&L, no posteriors.

Usage (after b0_signal_log.py has populated checkpoints/b0_firings/):
    python scripts/b0_audit.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (                                    # noqa: E402
    CHECKPOINT_DIR, STRATEGY_CLUSTERS_FILE, CLUSTER_CORR_FILE, REPORTS_DIR,
)

FIRINGS_DIR = CHECKPOINT_DIR / "b0_firings"
CORR_FLAG   = 0.70
CLUSTERS    = ["A", "B", "C", "D", "E"]


def _load_firings() -> pd.DataFrame:
    files = sorted(FIRINGS_DIR.glob("*.parquet"))
    if not files:
        raise SystemExit(f"No firing logs in {FIRINGS_DIR} — run scripts/b0_signal_log.py first.")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    print(f"[B0] loaded {len(df):,} fires from {len(files)} files")
    return df


def _direction_matrix(fires: pd.DataFrame) -> pd.DataFrame:
    """(stock,day) x strategy matrix of net direction in {-1,0,+1}."""
    fires = fires.copy()
    fires["key"] = fires["symbol"] + "|" + fires["date"].astype(str)
    # if a strategy fired both ways on a day (rare), take the net sign
    agg = fires.groupby(["key", "strategy"])["direction"].sum().clip(-1, 1)
    mat = agg.unstack("strategy").fillna(0.0)
    return mat


def _nearest_psd_corr(C: np.ndarray) -> np.ndarray:
    """Clip negative eigenvalues, renormalise to unit diagonal (guarantees eff() valid)."""
    C = (C + C.T) / 2
    vals, vecs = np.linalg.eigh(C)
    vals = np.clip(vals, 1e-6, None)
    C2 = vecs @ np.diag(vals) @ vecs.T
    d = np.sqrt(np.diag(C2))
    C2 = C2 / np.outer(d, d)
    np.fill_diagonal(C2, 1.0)
    return C2


def main():
    fires = _load_firings()
    s2c = json.loads(STRATEGY_CLUSTERS_FILE.read_text())["strategy_to_cluster"]

    mat = _direction_matrix(fires)
    n_obs = len(mat)
    print(f"[B0] {n_obs:,} (stock,day) observations, {mat.shape[1]} strategies")

    # ── pairwise strategy correlation ────────────────────────────────────────
    corr = mat.corr(method="pearson").fillna(0.0)
    flagged = []
    cols = list(corr.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            r = float(corr.iloc[i, j])
            if abs(r) > CORR_FLAG:
                flagged.append((cols[i], cols[j], round(r, 3)))
    flagged.sort(key=lambda x: -abs(x[2]))

    # ── inter-cluster correlation matrix C ───────────────────────────────────
    cluster_dir = pd.DataFrame(index=mat.index)
    for cl in CLUSTERS:
        members = [s for s in mat.columns if s2c.get(s) == cl]
        cluster_dir[cl] = mat[members].sum(axis=1).clip(-3, 3) if members else 0.0
    Craw = cluster_dir.corr(method="pearson").reindex(index=CLUSTERS, columns=CLUSTERS).fillna(0.0).to_numpy()
    C = _nearest_psd_corr(Craw)
    min_eig = float(np.linalg.eigvalsh(C).min())

    # eff() sanity on the estimated C
    def eff(v):
        v = np.array(v, float)
        return (v.sum() ** 2) / (v @ C @ v)

    # ── write cluster_corr.json (EMPIRICAL) ──────────────────────────────────
    out = {
        "_meta": {
            "status": "EMPIRICAL",
            "note": f"Estimated from {n_obs:,} (stock,day) training-period firings "
                    f"(<=2018). PSD-adjusted (min eigenvalue {min_eig:.4f}). "
                    f"Re-estimate at each WF freeze from training data only.",
            "n_observations": int(n_obs),
            "clusters": CLUSTERS,
        },
        "clusters": CLUSTERS,
        "matrix": [[round(float(x), 4) for x in row] for row in C],
    }
    CLUSTER_CORR_FILE.write_text(json.dumps(out, indent=2))

    # ── update high_correlation_pairs in strategy_clusters.json ──────────────
    sc = json.loads(STRATEGY_CLUSTERS_FILE.read_text())
    sc["high_correlation_pairs"]["confirmed"] = [[a, b, r] for a, b, r in flagged]
    STRATEGY_CLUSTERS_FILE.write_text(json.dumps(sc, indent=2))

    # ── audit report ─────────────────────────────────────────────────────────
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    fire_counts = fires.groupby("strategy").size().sort_values(ascending=False)
    lines = [
        "# B0 Correlation Audit", "",
        f"- observations: **{n_obs:,}** (stock,day) cells; **{len(fires):,}** total fires",
        f"- strategies observed: {mat.shape[1]}",
        f"- cluster matrix C min eigenvalue (post-PSD): {min_eig:.4f}", "",
        "## eff() on estimated C (plan gate examples)",
        f"- A,C confirm (binary): eff = {eff([1,0,1,0,0]):.3f}",
        f"- B,D confirm (binary): eff = {eff([0,1,0,1,0]):.3f}",
        f"- A,D confirm (binary): eff = {eff([1,0,0,1,0]):.3f}", "",
        f"## Correlated pairs (|r| > {CORR_FLAG})",
    ]
    if flagged:
        lines += [f"- {a} vs {b}: r = {r}" for a, b, r in flagged]
    else:
        lines += ["- none exceeded the threshold in this sample"]
    lines += ["", "## Inter-cluster matrix C", "",
              "| |" + "|".join(CLUSTERS) + "|", "|" + "---|" * (len(CLUSTERS) + 1)]
    for i, cl in enumerate(CLUSTERS):
        lines.append(f"|{cl}|" + "|".join(f"{C[i,j]:.2f}" for j in range(len(CLUSTERS))) + "|")
    lines += ["", "## Fire counts per strategy", ""]
    lines += [f"- {s}: {int(n)}" for s, n in fire_counts.items()]
    (REPORTS_DIR / "b0_audit.md").write_text("\n".join(lines))

    print(f"[B0] wrote {CLUSTER_CORR_FILE.name} (min eig {min_eig:.4f}), "
          f"{len(flagged)} flagged pairs, report -> reports/b0_audit.md")


if __name__ == "__main__":
    main()
