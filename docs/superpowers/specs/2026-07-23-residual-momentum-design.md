# Residual Momentum — design spec

**Date:** 2026-07-23
**Author:** Aaron + agent (brainstorm)
**Status:** proposed (awaiting review)
**Related:** the adaptive-input layer ([[adaptive-input-layer]]) supplies the reused
backtest/estimator machinery. This is a **one-time, backtested signal upgrade**, NOT
a perpetual dial (Aaron's call: strip-market-beta is a structural signal choice, not
a fast-drifting knob).

---

## 1. Purpose

Test whether ranking on **residual (idiosyncratic) momentum** — a stock's momentum
*after stripping out how much it just rode the market* — improves the book, and by
how much. Residual momentum's documented edge is **crash reduction** (you stop
loading high-beta names that all unwind together), so the deliverable is a
**comparison table** across the three lenses (return, risk-adjusted return,
drawdown/crash) that a human adjudicates — not a pre-committed threshold.

The current signal ranks on **total** return (`R/σ`). This spec adds the option to
blend in the **residual** version and validates it on deep, survivorship-free
history before any live change.

## 2. Scope

**In scope:** (1) a pure residual-momentum computation; (2) a `residual_tilt` blend
knob threaded through `momentum.compute` (default `0.0` = today's signal, exactly);
(3) a walk-forward comparison backtest over the survivorship-free pool that reports
the metric table across a `residual_tilt` grid.

**Explicitly out of scope:**
- **No perpetual/adaptive dial.** We pick the blend once from the backtest and
  (if adopted) set it in config; re-validate occasionally (quarterly-ish), not weekly.
- **No adoption decision baked in** — the spec produces the comparison; the human
  reads all three lenses and decides. Adoption = a later config edit.
- **Sector-beta variant** — first cut is **market-only** (SPY). Market+sector beta
  (using the 11 SPDR ETFs) is a noted follow-up variant, tried only if market-only
  is promising.
- No new data source (confirmed: `pool_closes.parquet` and `closes.parquet` already
  hold every name, SPY, and all 11 sector ETFs).

## 3. Design

### 3.1 The residual-momentum computation (the only real new math)

New pure module `src/residual.py`. For each ticker, as of a date, over the
`lookback` (252d) window of daily returns, with the market = SPY's daily returns:

```
β_i   = cov(r_i, r_mkt) / var(r_mkt)          # market beta over the window
α_i   = mean(r_i) − β_i · mean(r_mkt)
resid_t = r_i,t − (α_i + β_i · r_mkt,t)        # daily idiosyncratic return
residual_mom_i = Σ(resid_t) / std(resid_t)     # cumulative residual ÷ residual vol
```

This is the **direct analog of the existing `ret = R/σ`** (formation return ÷ daily
vol), just computed on the residual instead of the raw return. Pure, no I/O, no
clock (dates/panels passed in). Vectorized across all tickers per date with numpy
(betas from a single cov/var pass). Look-ahead is enforced the same way
`momentum.compute` does it (window ends at `asof`).

### 3.2 How the blend plugs in (`src/momentum.py`)

`compute(panel, asof, lookback=252, residual_tilt=0.0)` gains one param. The score
today is `mean(rank(ret), rank(trend))`. We blend the **momentum view's rank**:

```
p_ret   = rank(ret)                                  # current risk-adj return
p_resid = rank(residual_mom)                          # residual version
p_mom   = (1 − residual_tilt)·p_ret + residual_tilt·p_resid
score   = (p_mom + rank(trend)) / 2
```

Both are percentile ranks in [0,1], so blending them is clean (same scale).
**`residual_tilt = 0.0` reproduces today's score bit-for-bit** — fully backward
compatible; the backtest and live loop that call `compute` are unaffected until the
knob is set. The trend view is untouched.

### 3.3 The comparison backtest (the deliverable)

New script `scripts/backtest_residual.py`, reusing `scripts/backtest_pit.py`'s
survivorship-free `simulate()` — parameterized to run the signal at each
`residual_tilt ∈ {0, 0.25, 0.5, 0.75, 1.0}` over the pool (2021–2026, dead names
included). It emits **one comparison table**:

| residual_tilt | CAGR | Sharpe | MaxDD | 2022-unwind DD | held-into-death | vs w=0 |
|---|---|---|---|---|---|---|

`w=0` is the baseline (current signal); each row is read against it across all three
lenses. **Walk-forward is inherent** (the PIT backtest already ranks as-of each date
on trailing data only) — no value is fit and tested on the same window.

### 3.4 Where it runs

Off-box (the beta regressions over ~750 names × ~250 dates are the compute-heavy
part; keep them off the memory-tight droplet). A GitHub Actions `workflow_dispatch`
(manual) job, or a Codespaces run. **Build consideration:** the runner needs
`pool_closes.parquet` — either extend the ledger-mirror sync to include it
(`deploy/backup_ledger.sh`), or fetch fresh via `scripts/fetch_pool.py` (Alpaca
key as a secret). This is *not* a weekly cron — it's on-demand.

### 3.5 The adoption path (after the human reads the table)

If adopted, set `[signal] residual_tilt = <chosen w>` in `config/strategy.toml`
(or `strategy.local.toml` to trial it box-locally first). `momentum.compute` reads
it; because backtest AND live both call `compute`, the numbers never drift. Revert =
set back to `0.0`. Re-validate quarterly by re-running the comparison.

## 4. New vs reused

| Piece | Status |
|---|---|
| `src/residual.py` — residual-momentum math | **NEW** (the only real new code) |
| `residual_tilt` param in `momentum.compute` | small additive change |
| `scripts/backtest_residual.py` — the sweep | thin wrapper over `backtest_pit.simulate` |
| Survivorship-free pool + SPY + sector ETFs | **REUSE** (already cached) |
| Walk-forward OOS discipline | **REUSE** (inherent to the PIT backtest) |
| Off-box run pattern | **REUSE** (GitHub Actions, like the adaptive tuner) |

## 5. Constraints honored

- **Never trades.** Pure signal math + a research backtest; no order path. Nothing
  changes live until a human sets `residual_tilt` in config.
- **Backward compatible** — `residual_tilt=0.0` is today's signal exactly; the
  default is 0, so merging this changes nothing until deliberately tuned.
- **Deep-history validated** — the whole point of it being price-based (not moomoo):
  it runs on the survivorship-free pool across multiple regimes.
- **No drift** — backtest and live share `momentum.compute`.

## 6. Testing

- `src/residual.py --selftest` — the pure computation on hand-built fixtures: a name
  with β≈1 and no idiosyncratic drift → `residual_mom ≈ 0`; a name with a known
  idiosyncratic drift on top of the market → the expected sign/magnitude; a
  degenerate (zero market variance) input → safe (no divide-by-zero).
- `momentum.compute` regression: `residual_tilt=0.0` output is **identical** to the
  pre-change output on a fixture panel (backward-compat lock).
- `scripts/backtest_residual.py` — a small-panel smoke run producing the table
  structure; the numbers themselves are the research output, not an assertion.

## 7. Risks / watch-items

- **Overfit.** Even one knob (`w`) picked from a backtest can overfit. Mitigation:
  walk-forward is inherent; judge on the *shape* across `w` (is the curve smooth and
  sensible?), not a single lucky cell; the human adjudicates, we don't auto-adopt.
- **IEX-pool noise.** The pool's prices are Alpaca IEX (noisier, esp. for thin/dead
  names); residual regressions on thin names may be unstable. Watch for nonsense
  betas; consider a min-history / min-liquidity guard in the residual computation.
- **Beta instability.** A 252-day single-market beta is a rough estimate; fine for a
  ranking signal, but note it. Sector beta (future variant) may help or may overfit.
- **Look-ahead.** The residual regression must use only data ≤ `asof` — same
  discipline as `compute`; the selftest/backtest must not leak future returns.

## 8. Build order (each its own plan step)

1. `src/residual.py` — pure residual-momentum computation + `--selftest`.
2. Thread `residual_tilt` through `src/momentum.py::compute` (default 0.0);
   backward-compat regression test.
3. `scripts/backtest_residual.py` — sweep `residual_tilt` over the pool via
   `backtest_pit.simulate`, emit the comparison table.
4. Off-box run: make `pool_closes.parquet` available to the runner (mirror sync or
   `fetch_pool`), + a `workflow_dispatch` GitHub Action (or documented Codespaces run).
5. Run it, produce the table, **bring it back here to adjudicate** (all three lenses).

## 9. Future (not this spec)

- **Adopt** (if the table warrants): set `[signal] residual_tilt` in config.
- **Sector-beta variant** — regress on SPY *and* the name's SPDR sector ETF.
- **Periodic re-validation** — re-run the comparison quarterly.
