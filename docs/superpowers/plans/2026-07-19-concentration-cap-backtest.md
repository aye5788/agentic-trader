# Concentration Cap — Build & Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure correlation-based "cluster cap" that down-weights a co-moving cluster, wire it into the survivorship-free backtest, and sweep parameters to decide (drawdown-down / CAGR-flat) whether it's worth wiring live.

**Architecture:** A new pure module `src/concentration.py` (`cap_weights` + `_clusters`), consumed by a refactored `scripts/backtest_pit.py` that runs a baseline (cap off) plus a parameter sweep and prints a comparison table. NO live-path change in this phase — that's gated on the result.

**Tech Stack:** Python 3.12 (project `.venv`), pandas/numpy, the existing `momentum.py` (ranking), `backtest.py` helpers (`annualized`, `max_drawdown`, `weekly_rebalance_dates`), and the cached `research_store/prices/pool_closes.parquet`.

## Global Constraints

- `src/concentration.py` is PURE — its only external input is a price DataFrame passed in; no file/network I/O.
- **Cluster on POSITIVE correlation only** (`corr >= threshold`), never absolute — anti-correlated names diversify and must NOT be grouped together.
- `cap_weights` preserves total weight (fully invested) to floating-point tolerance and never changes membership (weights only).
- **No live-path change.** Do not touch `scripts/slow_loop.py` or any live loop in this phase.
- The refactor of `backtest_pit.py` must leave the **baseline (cap-off) numbers identical** to today's output.
- Selftests run under the project venv: `python` = `/opt/agentic-trader/.venv/bin/python`.

---

### Task 1: The pure concentration module

**Files:**
- Create: `src/concentration.py`

**Interfaces:**
- Produces:
  - `concentration._clusters(corr: pd.DataFrame, threshold: float) -> list[set[str]]` — connected components of the graph where `corr[i,j] >= threshold` (positive, i≠j); every column appears in exactly one set.
  - `concentration.cap_weights(weights: dict[str,float], closes: pd.DataFrame, asof, params: dict) -> dict[str,float]` — params keys: `lookback` (int), `corr_threshold` (float), `cluster_cap` (float). Returns adjusted weights summing to the same total.
  - `concentration._selftest() -> None`

- [ ] **Step 1: Write the module with a failing selftest**

Create `src/concentration.py`:
```python
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
```

- [ ] **Step 2: Run the selftest, verify it PASSES**

Run: `cd /opt/agentic-trader && .venv/bin/python src/concentration.py`
Expected: two lines —
```
concentration selftest OK: _clusters (positive-only)
concentration selftest OK: cap_weights (cap + redistribute + no-op)
```
(This is written to pass immediately — the code and its test are authored together. If an assert fails, the bug is in the implementation; fix `concentration.py` until both lines print.)

- [ ] **Step 3: Commit**

```bash
cd /opt/agentic-trader
git add src/concentration.py
git commit -m "feat(concentration): pure correlation cluster-cap (cap_weights + _clusters)"
```

---

### Task 2: Refactor backtest_pit into a reusable run + apply the cap

**Files:**
- Modify: `scripts/backtest_pit.py`

**Interfaces:**
- Consumes: `concentration.cap_weights` (Task 1); existing `momentum.compute/select`, `pit_universe`, `bt.annualized/max_drawdown/weekly_rebalance_dates`.
- Produces: `run_backtest(closes, dvol, candidates, etfs, spy, etf_panel, rebals, P, cap_params=None) -> dict` with keys `res` (DataFrame), `cagr`, `vol`, `sharpe`, `maxdd`, `total_return`, `avg_turnover`, `univ_sizes`, `dead_held`, `regime_frac`. `cap_params=None` = baseline (no cap).

**Goal of this task:** extract the per-rebalance loop into `run_backtest`, apply `cap_weights` to per-name weights when `cap_params` is given, and have `main()` call `run_backtest(cap_params=None)` and print the **exact same baseline table as today** (regression guard).

- [ ] **Step 1: Capture the current baseline output (regression anchor)**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/backtest_pit.py 2>&1 | tee /tmp/pit_baseline_before.txt | tail -15`
Expected: the current PIT table (CAGR ~23%, Sharpe ~0.97, etc.). Keep this file — Step 4 diffs against it. (If `pool_closes.parquet` is missing, run `scripts/fetch_pool.py` first per the script's own error message.)

- [ ] **Step 2: Add the import + extract `run_backtest`**

At the top of `scripts/backtest_pit.py` with the other `src` imports, add:
```python
import concentration as conc  # noqa: E402
```
Then extract the loop currently inside `main()` (the `for i in range(len(rebals) - 1):` block through the metrics) into a module-level function. Replace the per-leg `leg_ret(holds, per_slot)` with a per-name weights dict passed through the cap:
```python
def run_backtest(closes, dvol, candidates, etfs, spy, etf_panel, rebals, P, cap_params=None):
    """One PIT backtest run. cap_params=None -> baseline (equal slots, no cap);
    a params dict -> concentration.cap_weights applied to each week's weights."""
    book_w, sleeve_w = P["book_weight"], P["sleeve_weight"]
    book_hold, book_band, sleeve_hold = P["book_hold"], P["book_band"], P["sleeve_hold"]
    per_slot_book = book_w / book_hold
    per_slot_etf = sleeve_w / sleeve_hold

    held_book, held_etf = set(), set()
    equity = 1.0
    curve, turnover, univ_sizes, dead_held = [], [], [], []
    for i in range(len(rebals) - 1):
        t0, t1 = rebals[i], rebals[i + 1]
        univ = pit_universe(dvol, candidates, t0, closes)
        univ_sizes.append(len(univ))
        regime = mom.regime_on(spy, t0, 50)

        book_scored = mom.compute(closes[univ], t0)
        new_book = mom.select(book_scored, held_book, book_hold, book_band)
        if not regime:
            new_book = [t for t in new_book if t in held_book]
        etf_scored = mom.compute(etf_panel, t0)
        new_etf = mom.select(etf_scored, held_etf, sleeve_hold, sleeve_hold)

        turnover.append(len(set(new_book) ^ held_book) + len(set(new_etf) ^ held_etf))
        held_book, held_etf = set(new_book), set(new_etf)

        # equal slots -> optional concentration cap -> per-name weights
        weights = {t: per_slot_book for t in new_book}
        weights.update({t: per_slot_etf for t in new_etf})
        if cap_params is not None:
            weights = conc.cap_weights(weights, closes, t0, cap_params)

        def name_ret(t):
            p0 = closes.loc[t0, t] if t in closes.columns else np.nan
            p1 = closes.loc[t1, t] if t in closes.columns else np.nan
            if pd.isna(p0) or p0 <= 0:
                return 0.0
            return -1.0 if pd.isna(p1) else (p1 / p0 - 1)

        dead_held.append(sum(1 for t in new_book
                             if pd.notna(closes.loc[t0, t]) and pd.isna(closes.loc[t1, t])))
        port_ret = sum(wt * name_ret(t) for t, wt in weights.items())
        equity *= (1 + port_ret)
        curve.append((t1, equity, port_ret, len(new_book), len(new_etf), regime))

    res = pd.DataFrame(curve, columns=["date", "equity", "ret", "n_book", "n_etf", "regime"]
                       ).set_index("date")
    cagr, vol, sharpe = bt.annualized(res["ret"], 52.0)
    return {"res": res, "cagr": cagr, "vol": vol, "sharpe": sharpe,
            "maxdd": bt.max_drawdown(res["equity"]),
            "total_return": res["equity"].iloc[-1] - 1,
            "avg_turnover": float(np.mean(turnover)),
            "univ_sizes": univ_sizes, "dead_held": dead_held,
            "regime_frac": res["regime"].mean()}
```
**Note the equivalence to preserve:** the original summed `leg_ret(new_book, per_slot_book) + leg_ret(new_etf, per_slot_etf)`. With `cap_params=None` the weights dict is exactly `{book: per_slot_book, etf: per_slot_etf}`, and `port_ret = sum(w * name_ret)` equals the original sum — so baseline numbers are unchanged. Do not alter the NaN/delist handling (`p0<=0` skip, `p1` NaN → −1.0).

- [ ] **Step 3: Rewrite `main()` to call `run_backtest(cap_params=None)` and print the same table**

Replace the body of `main()` after the data-loading (the `for` loop + the metrics/printing) with a call to `run_backtest(..., cap_params=None)` and print the existing table from the returned dict. The printed lines must match the current format exactly — total return, CAGR, volatility, Sharpe, max drawdown, avg turnover/wk, pct weeks reg-on, avg universe — reading from `r["total_return"]`, `r["cagr"]`, `r["vol"]`, `r["sharpe"]`, `r["maxdd"]`, `r["avg_turnover"]`, `r["regime_frac"]`, `np.mean(r["univ_sizes"])`, and the SPY benchmark (`bench_eq`) exactly as today.

- [ ] **Step 4: Verify the baseline output is byte-identical**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/backtest_pit.py 2>&1 | tail -15 > /tmp/pit_baseline_after.txt; diff /tmp/pit_baseline_before.txt <(cd /opt/agentic-trader && .venv/bin/python scripts/backtest_pit.py 2>&1 | tail -15) && echo "BASELINE UNCHANGED"`
Expected: `BASELINE UNCHANGED` (the refactor changed structure, not numbers). If the diff is non-empty, the refactor altered behavior — reconcile before proceeding.

- [ ] **Step 5: Commit**

```bash
cd /opt/agentic-trader
git add scripts/backtest_pit.py
git commit -m "refactor(backtest-pit): extract run_backtest + optional concentration cap (baseline unchanged)"
```

---

### Task 3: The sweep + comparison table (the deliverable)

**Files:**
- Modify: `scripts/backtest_pit.py` (add `--sweep`)
- Modify: `deploy/run_selftests.sh`

**Interfaces:**
- Consumes: `run_backtest` (Task 2), `concentration` (Task 1).

- [ ] **Step 1: Add a `--sweep` mode**

Add `import argparse` and `import itertools` if not present. Add a `run_sweep()` that loads the data once (same loading block as `main()`), runs the baseline plus every combo, and prints one comparison table:
```python
SWEEP = {"lookback": [63, 126],
         "corr_threshold": [0.6, 0.7, 0.8],
         "cluster_cap": [0.30, 0.40, 0.50]}


def run_sweep():
    (closes, dvol, candidates, etfs, spy, etf_panel, rebals, P) = _load_pit_data()
    base = run_backtest(closes, dvol, candidates, etfs, spy, etf_panel, rebals, P, None)
    print(f"\n{'config':28}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}{'turn':>7}")
    print("-" * 59)
    print(f"{'BASELINE (no cap)':28}{base['cagr']:>7.1%}{base['sharpe']:>8.2f}"
          f"{base['maxdd']:>8.1%}{base['avg_turnover']:>7.1f}")
    combos = itertools.product(SWEEP["lookback"], SWEEP["corr_threshold"], SWEEP["cluster_cap"])
    for lb, th, cap in combos:
        cp = {"lookback": lb, "corr_threshold": th, "cluster_cap": cap}
        r = run_backtest(closes, dvol, candidates, etfs, spy, etf_panel, rebals, P, cp)
        tag = f"lb{lb} thr{th} cap{int(cap*100)}"
        print(f"{tag:28}{r['cagr']:>7.1%}{r['sharpe']:>8.2f}"
              f"{r['maxdd']:>8.1%}{r['avg_turnover']:>7.1f}"
              f"{'  <' if (r['maxdd'] > base['maxdd'] and r['cagr'] >= base['cagr'] - 0.02) else ''}")
```
Extract the shared data-loading (the `closes`/`dvol`/`pool`/`etfs`/`candidates`/`spy`/`etf_panel`/`rebals`/`P` block) into `_load_pit_data()` so both `main()` and `run_sweep()` use it (DRY). Wire argparse: `--sweep` calls `run_sweep()`, otherwise `main()` runs as today. (Note `maxdd` is negative, so "less bad" = closer to zero = `r['maxdd'] > base['maxdd']`; the `<` marks configs that cut drawdown without giving up >2 pts CAGR — the go/no-go shortlist.)

- [ ] **Step 2: Verify the sweep runs and produces the table**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/backtest_pit.py --sweep 2>&1 | tail -25`
Expected: the baseline row + 18 combo rows with CAGR/Sharpe/maxDD/turnover, some flagged `<`. Confirm the baseline row matches Task 2's baseline CAGR/Sharpe/maxDD (same numbers, since baseline is cap-off).

- [ ] **Step 3: Add the concentration selftest to the suite**

In `deploy/run_selftests.sh`, add `"src/concentration.py"` to the `SELFTESTS` array. (`concentration.py`'s `__main__` runs `_selftest()`, so `"$PY" src/concentration.py --selftest` — which ignores the flag — runs the selftest; confirm it prints the two OK lines.)

Run: `cd /opt/agentic-trader && deploy/run_selftests.sh 2>&1 | tail -8`
Expected: all selftests PASS, including the two concentration lines.

- [ ] **Step 4: Commit**

```bash
cd /opt/agentic-trader
git add scripts/backtest_pit.py deploy/run_selftests.sh
git commit -m "feat(backtest-pit): --sweep concentration-cap params + selftest wiring"
```

- [ ] **Step 5: Produce the deliverable (the decision input)**

Run the sweep once more and save the table:
`cd /opt/agentic-trader && .venv/bin/python scripts/backtest_pit.py --sweep 2>&1 | tee /tmp/concentration_sweep.txt`
This table (baseline vs. 18 capped configs on CAGR / Sharpe / max-drawdown / turnover) is the **go/no-go deliverable**. Summarize for Aaron: does any config cut max drawdown by ≥~3–5 points while giving up ≤~2 points CAGR with Sharpe ≥ baseline? If yes → recommend a Phase-2 live-wiring spec with that config. If no → recommend abandoning (concentration is the edge).

---

## Notes for the implementer

- **This phase touches NO live path.** `src/concentration.py` and `scripts/backtest_pit.py` only; never `slow_loop.py`.
- Everything runs under the project venv (`.venv/bin/python`); no moomoo, no network.
- The `<` flag in the sweep is a shortlist hint, not the decision — the human reads the full table.
- If the baseline diff in Task 2 Step 4 isn't clean, STOP and reconcile: the whole point is comparing cap-on vs an unchanged cap-off baseline.
