"""Dial-agnostic Bayesian estimator for one bounded, 1-D strategy knob.

A small grid of candidate values, each with a posterior over its OBJECTIVE
(spec §6.2: mean realized-R). Grid points are correlated by a squared-exponential
smoothness prior — a Gaussian process over the FIXED grid, so the posterior is
closed-form (pure numpy, no new deps). The recommendation only moves off the
incumbent when a challenger's posterior confidently dominates it; the posterior
WIDTH is the activation gate (spec §5). Overfit defense: `oos_gap` (spec §7).

Never executes. Emits a value for a human to promote (spec §3, §8).
Spec: docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md §5, §7
"""
from __future__ import annotations

from math import erf, sqrt

import numpy as np


def _rbf(grid: np.ndarray, length_scale: float, prior_var: float) -> np.ndarray:
    d = grid[:, None] - grid[None, :]
    return prior_var * np.exp(-(d ** 2) / (2.0 * length_scale ** 2))


def posterior(grid, obs_mean, obs_count, noise_var, prior_mean,
              length_scale, prior_var, jitter=1e-8):
    """Closed-form GP-over-grid posterior. Each grid point k with obs_count[k]>0
    contributes a noisy observation obs_mean[k] with variance noise_var/obs_count[k];
    the smoothness prior shares that evidence with neighbours."""
    grid = np.asarray(grid, float)
    n = len(grid)
    K = _rbf(grid, length_scale, prior_var) + jitter * np.eye(n)
    m0 = np.full(n, float(prior_mean))
    obs_idx = np.where(np.asarray(obs_count, float) > 0)[0]
    if obs_idx.size == 0:
        return m0.copy(), np.diag(K).copy(), K.copy()
    y = np.asarray(obs_mean, float)[obs_idx]
    D = np.diag(float(noise_var) / np.asarray(obs_count, float)[obs_idx])
    Koo = K[np.ix_(obs_idx, obs_idx)] + D
    Kso = K[:, obs_idx]
    Koo_inv = np.linalg.inv(Koo)
    post_mean = m0 + Kso @ Koo_inv @ (y - m0[obs_idx])
    post_cov = K - Kso @ Koo_inv @ Kso.T
    return post_mean, np.clip(np.diag(post_cov), 0.0, None).copy(), post_cov


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def recommend(grid, post_mean, post_cov, incumbent_idx, confidence=0.9):
    """Move off the incumbent only if the best-posterior-mean challenger confidently
    (>= confidence) beats the incumbent. P(challenger > incumbent) from the Normal
    difference of the two posterior marginals."""
    grid = np.asarray(grid, float)
    pm = np.asarray(post_mean, float)
    C = np.asarray(post_cov, float)
    cand = int(np.argmax(pm))
    if cand == incumbent_idx:
        return {"recommended_idx": incumbent_idx,
                "recommended_value": float(grid[incumbent_idx]),
                "p_better": 0.0, "moved": False}
    mean_diff = pm[cand] - pm[incumbent_idx]
    var_diff = C[cand, cand] + C[incumbent_idx, incumbent_idx] - 2.0 * C[cand, incumbent_idx]
    sd = sqrt(max(float(var_diff), 1e-12))
    p = _normal_cdf(mean_diff / sd)
    moved = bool(p >= confidence)
    idx = cand if moved else incumbent_idx
    return {"recommended_idx": idx, "recommended_value": float(grid[idx]),
            "p_better": float(p), "moved": moved}


def oos_gap(train_mean, holdout_mean, best_idx):
    """Overfit alarm: in-sample objective minus out-of-sample objective at the
    value the in-sample data recommends. Large positive gap = optimism = overfit."""
    return float(np.asarray(train_mean, float)[best_idx]
                 - np.asarray(holdout_mean, float)[best_idx])


def _selftest() -> None:
    grid = np.array([1.5, 2.0, 2.5, 3.0, 3.5])
    zero = np.zeros(len(grid))

    # 1. No observations -> posterior == prior everywhere; recommend holds incumbent.
    pm, pv, pc = posterior(grid, obs_mean=zero, obs_count=zero,
                           noise_var=1.0, prior_mean=0.3, length_scale=0.5, prior_var=1.0)
    assert np.allclose(pm, 0.3), pm
    rec = recommend(grid, pm, pc, incumbent_idx=2)   # incumbent = 2.5
    assert rec["moved"] is False and rec["recommended_value"] == 2.5, rec

    # 2. Smoothness: strong evidence at idx 3 (value 3.0) pulls its unobserved
    #    neighbour idx 4 above the prior, and tightens the neighbour's variance.
    om = zero.copy(); oc = zero.copy()
    om[3] = 2.0; oc[3] = 200.0
    pm2, pv2, pc2 = posterior(grid, om, oc, noise_var=1.0,
                              prior_mean=0.3, length_scale=0.6, prior_var=1.0)
    assert pm2[4] > 0.35, pm2                 # neighbour dragged up from prior 0.3
    assert pv2[4] < pv[4], (pv2[4], pv[4])    # neighbour uncertainty shrank

    # 3. Smoothing beats an independent grid at the neighbour (lower variance).
    _, pv_indep, _ = posterior(grid, om, oc, noise_var=1.0,
                               prior_mean=0.3, length_scale=1e-6, prior_var=1.0)
    assert pv2[4] < pv_indep[4], (pv2[4], pv_indep[4])

    # 4. Gate MOVES on a clear hill peaked at 3.0. Note: because the smoothness
    #    prior CORRELATES adjacent points, distinguishing them needs DIRECT
    #    evidence at the incumbent too (not just at the challenger) — a lone strong
    #    point next door gets partly shared with the incumbent and won't clear the
    #    bar. Here 2.5 is directly shown mediocre and 3.0/3.5 directly strong.
    om3 = zero.copy(); oc3 = zero.copy()
    om3[2] = 0.2; oc3[2] = 400.0              # 2.5 (incumbent) directly mediocre
    om3[3] = 1.5; oc3[3] = 400.0              # 3.0 directly best
    om3[4] = 1.4; oc3[4] = 400.0              # 3.5 nearly as good (a hill, not a spike)
    pm3, _, pc3 = posterior(grid, om3, oc3, noise_var=1.0,
                            prior_mean=0.3, length_scale=0.6, prior_var=1.0)
    rec3 = recommend(grid, pm3, pc3, incumbent_idx=2, confidence=0.9)
    assert rec3["moved"] is True and rec3["recommended_value"] == 3.0, rec3
    assert rec3["p_better"] >= 0.9, rec3

    # 5. Weak evidence (tiny count) does NOT move the dial.
    om4 = zero.copy(); oc4 = zero.copy()
    om4[3] = 1.5; oc4[3] = 2.0
    pm4, _, pc4 = posterior(grid, om4, oc4, noise_var=1.0,
                            prior_mean=0.3, length_scale=0.6, prior_var=1.0)
    rec4 = recommend(grid, pm4, pc4, incumbent_idx=2, confidence=0.9)
    assert rec4["moved"] is False, rec4

    # 6. oos_gap: in-sample optimism is positive when holdout underperforms.
    gap = oos_gap(train_mean=np.array([0.0, 0.0, 0.0, 1.0, 0.0]),
                  holdout_mean=np.array([0.0, 0.0, 0.0, 0.2, 0.0]), best_idx=3)
    assert abs(gap - 0.8) < 1e-9, gap

    print("selftest OK: posterior(prior/smoothing/independent), recommend(gate), oos_gap")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
