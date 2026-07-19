# Concentration Cap — Build & Backtest (Piece 2, Phase 1)

**Date:** 2026-07-19
**Status:** Approved design, pre-implementation
**Author:** Aaron + Claude (brainstorming session)

---

## 1. Problem

The book concentrates into whatever theme leads (momentum's nature) amplified by a
tech-heavy universe. On 2026-07-17 the storage/semi cluster — ~8 of 10 book names —
fell together and stopped out at once (equity $81.99 → $71.47, ~−13% in three
sessions). That "everything sinks together" event is the tail risk this piece
targets.

**Objective (chosen):** *cap the tail, keep the edge.* Prevent a co-moving cluster
from dominating capital, while giving up little/no CAGR. This is a minimal-touch
overlay, not a re-design of the momentum engine.

**This is Piece 2 of the selection-engine thread.** Piece 1 (universe maintenance)
shipped and is independent of this. Piece 3 (short-term TI overlay) is separate.

## 2. Scope of THIS spec — build & backtest ONLY

**In scope:** a pure "cluster cap" weighting function + wire it into the
survivorship-free backtest (`backtest_pit.py`) + a parameter sweep + a
capped-vs-uncapped results table.

**Explicitly OUT of scope (gated on the backtest result):** any change to the LIVE
path (`slow_loop.py`). We do not wire this into live trading until the backtest
proves it reduces drawdown without gutting CAGR. If the numbers are bad, we abandon
it — a "no" is a valid, valuable outcome (it would mean the concentration *is* the
edge; keep managing it via the existing stops). Live wiring is a separate Phase-2
spec, written only if Phase 1 passes.

## 3. How concentration is measured & capped

**Detection = realized correlation** (not sector labels — no taxonomy needed):
- From the cached daily closes, compute trailing-window daily-return correlation
  among the current holdings (book + sleeve together — a sector ETF like XLK
  co-moves with the semis, so it belongs in the same cluster).
- **Cluster** = a connected group of holdings whose pairwise correlation ≥
  `corr_threshold`. Concretely: build the graph where an edge connects two holdings
  with correlation ≥ threshold; each connected component is a cluster. Simple and
  interpretable for ~14 names.

**Action = down-weight the cluster (never change membership):**
- Start from the book/sleeve's equal-weight slots (the current
  `per_slot = leg_weight / hold`).
- For any cluster whose **aggregate** weight exceeds `cluster_cap`, scale its
  members down pro-rata so the cluster sums to exactly `cluster_cap`; collect the
  freed weight.
- **Redistribute** the freed weight to the holdings *outside* capped clusters,
  pro-rata to their current weights, so the book stays fully invested. If no
  holding is outside a capped cluster (degenerate: everything is one big cluster),
  redistribute to the holdings with the **lowest average correlation to the capped
  cluster** — there is always somewhere less-correlated to put it, so it never
  silently no-ops into cash.
- Weights only. It never adds/drops a name and never parks money in cash.

**No-op case:** if no cluster exceeds the cap, weights are returned unchanged.

## 4. The pure function (the single source of truth)

`src/concentration.py` (new, pure — no I/O beyond taking a price panel in):

```
cap_weights(weights: dict[str, float],
            closes: pd.DataFrame,   # daily closes, index=dates, cols=tickers
            asof,                    # rebalance date
            params: dict) -> dict[str, float]
```
- Returns adjusted `{ticker: weight}` summing to the same total as the input
  (fully invested; total preserved to floating-point tolerance).
- Pure and deterministic given `(weights, closes, asof, params)`.
- **Called by BOTH `backtest_pit.py` and (later, if approved) the live slow loop**,
  so backtest and live can never diverge — the same discipline that keeps
  `momentum.py` honest.
- Helper `_clusters(corr, threshold) -> list[set[str]]` (connected components) kept
  separate and unit-tested.

Correlation input: `returns = closes.loc[:asof, holdings].pct_change()`, take the
last `lookback` rows, `.corr()`. Names with insufficient history are treated as
uncorrelated (own singleton cluster) — they can't be shown to co-move.

## 5. Parameters (swept by the backtest, not guessed)

| Param | Sweep values | Meaning |
|---|---|---|
| `lookback` | 63, 126 | trading days of returns for the correlation window (~3mo, ~6mo) |
| `corr_threshold` | 0.6, 0.7, 0.8 | pairwise correlation that counts as "co-moving" |
| `cluster_cap` | 0.30, 0.40, 0.50 | max aggregate weight for one cluster |

18 combos + 1 baseline (cap off). Defaults if we proceed: whatever the sweep favors.

## 6. Backtest integration

Modify `scripts/backtest_pit.py`:
- Where it currently applies uniform `per_slot` weights in `leg_ret`, first build a
  combined `{ticker: per_slot}` map for the week's holdings (book + sleeve), pass it
  through `cap_weights(...)` (when the cap is enabled), and use the returned
  per-name weights in the return calc.
- Add a way to run **cap-off (baseline)** and **cap-on with given params** — a
  small internal sweep loop or CLI flags. Reuses the existing `closes`
  (`pool_closes.parquet`), rebalance dates, `momentum.compute/select`, and the
  `bt.annualized` / `bt.max_drawdown` metrics — no new data.
- Emit one comparison table: for baseline and each param combo — CAGR, vol, Sharpe,
  **max drawdown**, avg turnover — alongside SPY.

The correlation history is available: `pool_closes.parquet` floors ~2020-07 and the
backtest runs 2021-2026, so every rebalance has ≥6 months of history for the window.

## 7. Success criterion (the go/no-go gate)

**PASS (→ write Phase 2 live-wiring spec):** at least one config meaningfully cuts
max drawdown (target ≥ ~3–5 points) while CAGR gives up little (target ≤ ~2 points)
and Sharpe ≥ baseline.

**FAIL (→ abandon; recommend NOT wiring live):** every config that cuts drawdown
also craters CAGR (concentration is the edge). Report this plainly; it's a real
result, not a failure of the work.

The decision is Aaron's, on the numbers — this spec just produces them.

## 8. Testing

`src/concentration.py --selftest` (or via a `scripts/` selftest, matching repo
convention — pure functions, run under `.venv/bin/python`):
- `_clusters`: a crafted correlation matrix → correct connected components at a
  threshold (two correlated pairs + one loner → right grouping).
- `cap_weights`:
  - an over-cap cluster is scaled to exactly `cluster_cap`; freed weight lands on
    the uncorrelated holdings; total weight preserved.
  - no cluster over cap → weights returned unchanged (no-op).
  - degenerate all-one-cluster → redistributes to least-correlated, total preserved.
  - a name with insufficient history is treated as its own singleton (not capped).
- Added to `deploy/run_selftests.sh`.

The backtest run itself is the integration test (produces the table).

## 9. Files

- **Create:** `src/concentration.py` (pure: `cap_weights`, `_clusters`, `_selftest`).
- **Modify:** `scripts/backtest_pit.py` (apply `cap_weights` in weighting; add
  baseline-vs-cap sweep + comparison table).
- **Modify:** `deploy/run_selftests.sh` (add the concentration selftest).
- **Deliverable (not a file):** the sweep results table + a go/no-go recommendation.

## 10. Out of scope / follow-on

- Live wiring into `slow_loop.py` — Phase 2, only if Phase 1 passes.
- Any sector-taxonomy work — not needed (correlation-based).
- Survivorship caveats of `backtest_pit.py` are inherited as-is (this compares
  capped vs uncapped on the *same* honest PIT engine, so the *relative* result is
  robust even though absolute levels carry the usual caveat).

## 11. Risks / open questions

- **Clustering method:** connected-components at a threshold is simple but a single
  strong link can chain two loosely-related names into one cluster. Acceptable for
  v1 (a chained cluster is still genuinely co-moving pairwise); note it, and if the
  sweep looks odd, revisit with average-linkage.
- **Redistribution can lift a lower-ranked name's weight** above a capped
  top-ranked name's. That's intended (the point is to de-concentrate), but worth
  eyeballing in the backtest that it doesn't do anything perverse.
- **Turnover:** re-weighting weekly could add turnover/cost the current backtest
  doesn't model. Report avg turnover in the table so we can see it.
