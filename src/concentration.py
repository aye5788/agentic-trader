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


def _waterfill(w: dict, order: list, freed: float, ceiling: float) -> float:
    """Pour `freed` weight onto the names in `order`, pro-rata to current weight but
    never lifting any above `ceiling`; overflow from a name that hits the ceiling
    spills to the still-under-ceiling names, repeated until placed or all are full.
    Mutates `w` in place; returns any weight that could not be placed (everyone full)."""
    tol = 1e-12
    while freed > tol:
        under = [n for n in order if w[n] < ceiling - tol]
        if not under:
            break
        base = sum(w[n] for n in under)
        placed = 0.0
        for n in under:
            share = freed * (w[n] / base) if base > tol else freed / len(under)
            add = min(share, ceiling - w[n])
            w[n] += add
            placed += add
        freed -= placed
        if placed <= tol:            # numerical stall — everyone effectively full
            break
    return freed


def cap_weights(weights: dict, closes: pd.DataFrame, asof, params: dict) -> dict:
    """Down-weight any positively-correlated cluster whose aggregate weight exceeds
    params['cluster_cap'] * total; redistribute the freed weight to holdings OUTSIDE
    capped clusters (pro-rata), or — if everything is capped — to the least-correlated
    holding. Total weight preserved (fully invested). Membership unchanged.

    When params['per_name_cap'] is set (absolute weight, e.g. the live
    [risk] max_weight_per_name), redistribution water-fills so NO name is lifted above
    that ceiling; if receivers fill up before the freed weight is placed, the remainder
    spills back onto the (scaled-down) cluster members — the mandate ceiling and staying
    fully-invested both win over hitting cluster_cap exactly. Absent/None -> the
    unbounded pro-rata redistribution (backtest baseline behaviour), unchanged."""
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
    ceiling = params.get("per_name_cap")
    if ceiling is not None:
        # water-fill onto receivers first, then spill any remainder back onto the
        # scaled-down cluster members (both stay <= ceiling, total preserved).
        leftover = _waterfill(w, receivers, freed, float(ceiling))
        if leftover > 1e-12:
            leftover = _waterfill(w, [n for n in names if n not in receivers],
                                  leftover, float(ceiling))
        return w
    if receivers:
        base = sum(w[n] for n in receivers)
        for n in receivers:
            w[n] += freed * (w[n] / base) if base > 0 else freed / len(receivers)
    else:  # degenerate: everything capped — give it to the least-correlated name
        avg = {n: (corr[n].drop(labels=[n], errors="ignore").mean() if n in corr else 0.0)
               for n in names}
        w[min(avg, key=avg.get)] += freed
    return w


def _clusters_report_selftest() -> None:
    """⛔ clusters_report() was PUBLISHED BY positions() WITH NO TEST. The
    module's selftest is defined above it and never touched it, so the model
    the agent reads was unvalidated -- exactly the gate ("only after their
    model contracts are approved") it was supposed to satisfy. Found by
    review, 2026-08-18."""
    import numpy as _np
    import pandas as _pd
    idx = _pd.date_range("2026-01-01", periods=200, freq="D")
    rng = _np.random.default_rng(0)
    base = _np.cumsum(rng.normal(0, 1, 200)) + 100
    other = _np.cumsum(rng.normal(0, 1, 200)) + 100
    closes = _pd.DataFrame({
        "AA": base,                       # AA and AB move together
        "AB": base + rng.normal(0, 0.01, 200),
        "ZZ": other,                      # ZZ is independent
    }, index=idx)
    r = clusters_report(["AA", "AB", "ZZ"], closes, idx[-1])
    assert r["members"]["AA"] == ["AA", "AB"], r["members"]
    assert r["members"]["ZZ"] == ["ZZ"], r["members"]
    # the whole contract must travel with the answer, or the number is
    # uninterpretable and reads as a sector classification
    for key in ("version", "method", "lookback_days", "correlation_threshold",
                "min_observations_rule", "price_panel_asof",
                "symbols_not_in_panel", "note"):
        assert key in r["model"], key
    # a symbol absent from the panel is a singleton and is NAMED as absent
    r2 = clusters_report(["AA", "NOPE"], closes, idx[-1])
    assert r2["members"]["NOPE"] == ["NOPE"], r2["members"]
    assert "NOPE" in r2["model"]["symbols_not_in_panel"], r2["model"]
    # too little history -> singleton, and said so rather than grouped silently
    short = closes.tail(5)
    r3 = clusters_report(["AA", "AB"], short, short.index[-1])
    assert r3["members"]["AA"] == ["AA"], r3["members"]
    assert set(r3["model"]["symbols_with_insufficient_history"]) == {"AA", "AB"}, r3["model"]
    # fewer than two names cannot cluster and must not raise
    assert clusters_report(["AA"], closes, idx[-1])["members"] == {"AA": ["AA"]}
    assert clusters_report([], closes, idx[-1])["members"] == {}
    print("selftest OK: clusters_report — co-movers grouped, independents and "
          "short-history names singletons, absent symbols named, full model "
          "contract attached")


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

    # per_name_cap: freed weight must never push a receiver above an absolute
    # per-name ceiling (the live [risk] max_weight_per_name). Craft a heavy
    # cluster + thin receivers so unbounded redistribution WOULD overshoot.
    heavy = {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.15, "E": 0.10}   # ABC cluster = 0.75
    p_heavy = {"lookback": n, "corr_threshold": 0.7, "cluster_cap": 0.50}
    w_noceil = cap_weights(heavy, closes, asof, p_heavy)
    assert abs(sum(w_noceil.values()) - 1.0) < 1e-9, w_noceil
    assert max(w_noceil.values()) > 0.20 + 1e-9, w_noceil            # a receiver overshoots ~0.30
    w_ceil = cap_weights(heavy, closes, asof, {**p_heavy, "per_name_cap": 0.20})
    assert abs(sum(w_ceil.values()) - 1.0) < 1e-9, w_ceil            # still fully invested
    assert max(w_ceil.values()) <= 0.20 + 1e-9, w_ceil              # nobody breaches the ceiling
    # ceiling absent -> behaviour is exactly the None default (backtest baseline intact)
    assert cap_weights(heavy, closes, asof, {**p_heavy, "per_name_cap": None}) == w_noceil
    print("concentration selftest OK: cap_weights (per_name_cap water-fill)")



# --- Reporting membership, which is NOT the same use as capping weights -------
# The concentration CAP was backtested and abandoned on 2026-07-20 (it fought
# the live 10% per-name ceiling). What follows does not cap anything: it reports
# which held names co-move, as a measurement.
#
# Codex, 2026-08-18, on why this must carry its parameters: a field called
# `cluster` "must not pretend to mean 'sector' or the agent's semantic
# 'semi/storage complex'", and correlation clustering is honest only if the
# output also identifies its lookback, threshold, panel as-of, minimum-
# observation rule and model version. All five travel with the result below.

CLUSTER_MODEL_VERSION = "corr-connected-components/1"
# From the PIT backtest's own sweep grid (scripts/backtest_pit.py SWEEP:
# lookback [63, 126], corr_threshold [0.6, 0.7, 0.8]) rather than picked here.
CLUSTER_DEFAULT_LOOKBACK = 126
CLUSTER_DEFAULT_THRESHOLD = 0.7


def clusters_report(symbols, closes, asof, lookback: int = CLUSTER_DEFAULT_LOOKBACK,
                    threshold: float = CLUSTER_DEFAULT_THRESHOLD) -> dict:
    """Which held names co-move, with the whole model contract attached. Pure.

    Returns {"members": {symbol: [names in its cluster]},
             "cluster_id": {symbol: id},
             "model": {...the five things that make it interpretable...}}

    A name with too little history, or one absent from the panel, is its own
    singleton and is reported as such rather than silently grouped.
    """
    names = sorted({s for s in symbols})
    present = [n for n in names if n in getattr(closes, "columns", [])]
    model = {
        "version": CLUSTER_MODEL_VERSION,
        "method": "connected components over pairwise Pearson correlation of "
                  "daily returns, positive correlation only",
        "lookback_days": int(lookback),
        "correlation_threshold": float(threshold),
        "min_observations_rule": f"a name needs >= {max(2, int(lookback) // 2)} "
                                 f"non-null daily returns, else it is a singleton",
        "price_panel_asof": str(asof),
        "symbols_not_in_panel": sorted(set(names) - set(present)),
        "note": "correlation membership is a MEASUREMENT, not a sector "
                "classification and not a limit. The mandate sets no cluster cap.",
    }
    if len(present) < 2:
        return {"members": {n: [n] for n in names},
                "cluster_id": {n: n for n in names}, "model": model}

    hist = closes.loc[:asof, present]
    rets = hist.pct_change().tail(int(lookback))
    min_obs = max(2, int(lookback) // 2)
    usable = [c for c in rets.columns if rets[c].notna().sum() >= min_obs]
    model["symbols_with_insufficient_history"] = sorted(set(present) - set(usable))

    members, cid = {}, {}
    if len(usable) >= 2:
        corr = rets[usable].corr()
        for comp in _clusters(corr, float(threshold)):
            grp = sorted(comp)
            key = grp[0]
            for n in grp:
                members[n] = grp
                cid[n] = key
    for n in names:                       # everything else is its own singleton
        members.setdefault(n, [n])
        cid.setdefault(n, n)
    return {"members": members, "cluster_id": cid, "model": model}


# ⛔ THE ENTRY POINT MUST BE LAST. It previously sat above clusters_report(), so
# running this module executed a selftest for a function that did not yet exist
# -- the published cluster model was therefore never validated by its own file.
# Found by review, 2026-08-18.
if __name__ == "__main__":
    _selftest()
    _clusters_report_selftest()
