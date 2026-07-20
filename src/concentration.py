"""Concentration cap — down-weight a co-moving cluster so no correlated group
dominates capital. PURE: takes a price panel in, returns adjusted weights out.
Shared by scripts/backtest_pit.py and (only if the backtest approves) the live loop,
so backtest and live can never diverge.

Clusters on POSITIVE correlation only: anti-correlated names diversify risk and must
not be grouped. Weights only — membership is never changed; stays fully invested.
"""
import pandas as pd


def _clusters(corr: pd.DataFrame, threshold: float) -> list:
    """Connected components of the graph where corr[i,j] >= threshold (i != j).
    Positive correlation only. Every column of `corr` appears in exactly one set;
    a name correlated with no other is its own singleton."""
    names = list(corr.columns)
    seen, out = set(), []
    for start in names:
        if start in seen:
            continue
        stack, comp = [start], set()
        while stack:
            n = stack.pop()
            if n in comp:
                continue
            comp.add(n)
            seen.add(n)
            for m in names:
                if m != n and m not in comp and float(corr.at[n, m]) >= threshold:
                    stack.append(m)
        out.append(comp)
    return out


def cap_weights(weights: dict, closes: pd.DataFrame, asof, params: dict) -> dict:
    """Down-weight any positively-correlated cluster whose aggregate weight exceeds
    params['cluster_cap'] * total; redistribute the freed weight to holdings OUTSIDE
    capped clusters (pro-rata), or — if everything is capped — to the least-correlated
    holding. Total weight preserved (fully invested). Membership unchanged."""
    names = [t for t in weights if weights[t] > 0]
    if len(names) < 2:
        return dict(weights)
    total = sum(weights[t] for t in names)

    hist = closes.loc[:asof, [n for n in names if n in closes.columns]]
    rets = hist.pct_change().tail(int(params["lookback"]))
    # a name needs enough history to judge co-movement; otherwise it's a loner
    min_obs = max(2, int(params["lookback"]) // 2)
    usable = [c for c in rets.columns if rets[c].notna().sum() >= min_obs]
    w = dict(weights)
    if len(usable) < 2:
        return w
    corr = rets[usable].corr()
    clusters = _clusters(corr, float(params["corr_threshold"]))
    clusters += [{n} for n in names if n not in usable]   # unusable → singletons

    cap = float(params["cluster_cap"]) * total
    freed, capped = 0.0, set()
    for cl in clusters:
        cl = {n for n in cl if n in w}
        agg = sum(w[n] for n in cl)
        if len(cl) >= 2 and agg > cap:
            scale = cap / agg
            for n in cl:
                freed += w[n] * (1.0 - scale)
                w[n] *= scale
            capped |= cl
    if freed <= 0:
        return w

    receivers = [n for n in names if n not in capped]
    if receivers:
        base = sum(w[n] for n in receivers)
        for n in receivers:
            w[n] += freed * (w[n] / base) if base > 0 else freed / len(receivers)
    else:  # degenerate: everything capped — give it to the least-correlated name
        avg = {n: (corr[n].drop(labels=[n], errors="ignore").mean() if n in corr else 0.0)
               for n in names}
        w[min(avg, key=avg.get)] += freed
    return w


def _selftest() -> None:
    import numpy as np

    # _clusters: A,B,C mutually >=0.7; D isolated -> two components
    c = pd.DataFrame(
        [[1.0, 0.9, 0.8, 0.1],
         [0.9, 1.0, 0.85, 0.0],
         [0.8, 0.85, 1.0, 0.2],
         [0.1, 0.0, 0.2, 1.0]],
        index=list("ABCD"), columns=list("ABCD"))
    got = _clusters(c, 0.7)
    got = sorted([tuple(sorted(s)) for s in got])
    assert got == [("A", "B", "C"), ("D",)], got
    # anti-correlation is NOT a cluster (positive-only)
    c2 = pd.DataFrame([[1.0, -0.9], [-0.9, 1.0]], index=list("AB"), columns=list("AB"))
    assert sorted(len(s) for s in _clusters(c2, 0.7)) == [1, 1]
    print("concentration selftest OK: _clusters (positive-only)")

    # cap_weights: A,B,C share one return series (corr=1); D,E independent
    n = 40
    idx = pd.date_range("2021-01-01", periods=n + 1, freq="D")
    rng = np.random.default_rng(0)
    ra, rd, re = (rng.normal(0.001, 0.02, n) for _ in range(3))

    def px(r):
        return pd.Series(100.0 * np.cumprod(np.r_[1.0, 1.0 + r]), index=idx)

    closes = pd.DataFrame({"A": px(ra), "B": px(ra), "C": px(ra), "D": px(rd), "E": px(re)})
    asof = idx[-1]
    weights = {k: 0.2 for k in "ABCDE"}                     # total 1.0
    params = {"lookback": n, "corr_threshold": 0.7, "cluster_cap": 0.5}
    w = cap_weights(weights, closes, asof, params)
    assert abs(sum(w.values()) - 1.0) < 1e-9, w                       # fully invested
    assert abs((w["A"] + w["B"] + w["C"]) - 0.5) < 1e-6, w            # cluster capped to 0.5
    assert abs((w["D"] + w["E"]) - 0.5) < 1e-6, w                     # freed weight to loners
    # no-op when the cap is above the cluster's weight
    w2 = cap_weights(weights, closes, asof, {**params, "cluster_cap": 0.7})
    assert all(abs(w2[k] - weights[k]) < 1e-9 for k in weights), w2
    print("concentration selftest OK: cap_weights (cap + redistribute + no-op)")


if __name__ == "__main__":
    _selftest()
