# Sector-Residual Momentum — design spec

**Date:** 2026-07-23
**Author:** Aaron + agent (brainstorm)
**Status:** proposed (awaiting review)
**Extends:** [[residual-momentum]] (market-only, merged 5f5fdd3). Market-only residual
momentum was marginal and did NOT deliver the crash-reduction thesis (MaxDD got
*worse* with tilt). This variant strips **sector** rotation too, to test whether
that revives the benefit.

---

## 1. Purpose

Test residual momentum computed against the **11 SPDR sector ETFs** instead of just
the market, and produce a single table comparing **market-only vs sector** residual
across the tilt grid — so we can see whether stripping sector-rotation exposure (the
version with the stronger academic track record) improves risk-adjusted return and
*reduces* drawdown where market-only did not.

## 2. Scope

**In scope:** (1) a pure multi-factor residual computation (regress on the 11 sector
ETF returns); (2) generalize `momentum.compute` to accept a multi-factor `factors`
set (still `residual_tilt=0.0` = no-op); (3) extend `scripts/backtest_residual.py`
to run **both** variants and print the combined comparison.

**Out of scope:**
- **No per-name sector map.** Regressing on all 11 sectors at once lets OLS find each
  stock's loadings — no classification needed, so it works for dead pool names.
- Adoption decision (a later human config edit); still inert at `residual_tilt=0.0`.
- No perpetual dial; no other factors (size/value) — sectors only.

## 3. Design

### 3.1 Multi-factor residual (`src/residual.py`, added alongside the existing 1-factor fns)

The signal is unchanged in spirit — `residual_mom = α / std(resid)` — but the
regression is now multivariate on the 11 sector ETF returns (with an intercept).
Vectorized across all names via one least-squares solve (the design matrix is shared):

```
X = [1 | F]                     # T × (1+11): intercept + 11 sector-ETF returns
B = lstsq(X, Y)                 # (1+11) × N solved once for all clean names
resid = Y − X·B                 # T × N
α = B[0]                        # per-name intercept = idiosyncratic drift
residual_mom = α / std(resid)
```

- **`np.linalg.lstsq` (SVD-based)** — stable under the sector ETFs' collinearity: the
  residual/fitted are well-defined even where individual betas aren't. 12 params on
  ~252 obs is well-conditioned.
- **NaN handling:** solve only the columns (names) with a complete non-NaN window
  (the same set `momentum.compute`'s `valid` filter keeps); names with gaps → NaN
  residual_mom (blended path falls back to `p_ret`). Sector ETFs are ~always present,
  so the shared factor rows are clean.
- **No look-ahead:** window ends at `asof`, same discipline as the 1-factor version.

New pure functions:
- `residual_mom_multifactor(rets: pd.DataFrame, factor_rets: pd.DataFrame) -> pd.Series`
- `residual_momentum_sector(panel, asof, factor_panel: pd.DataFrame, lookback=252) -> pd.Series`
  (extracts the trailing window from close panels and calls the core).

### 3.2 The factors — 11 SPDR sector ETFs, no separate SPY

`XLE, XLF, XLK, XLV, XLI, XLP, XLY, XLU, XLB, XLRE, XLC` (all confirmed present in
both `pool_closes.parquet` and `closes.parquet`). They collectively span market +
sector movement, so a separate SPY factor would be redundant/collinear. Residual =
return unexplained by *any* sector = stock-specific.

### 3.3 Integration (`momentum.compute`)

Generalize the residual path: `compute(panel, asof, lookback=252, residual_tilt=0.0,
market=None, factors=None)`. When `residual_tilt > 0`:
- if `factors` is not None → multi-factor (sector) residual via `residual_momentum_sector`;
- elif `market` is not None → single-market residual (existing behavior).
`residual_tilt=0.0` remains the verified bit-for-bit no-op; `market`-only callers are
unchanged.

### 3.4 The deliverable (`scripts/backtest_residual.py`)

Extend the sweep to run **both** variants over the survivorship-free pool and print
one table:
```
tilt   [market-only] CAGR Sharpe MaxDD 2022DD   [sector] CAGR Sharpe MaxDD 2022DD
```
`run_backtest` gains a `factors=None` param threaded to the book compute (alongside
the existing `residual_tilt`); the sector rows pass the 11-ETF close panel as
`factors`. Human adjudicates market-only vs sector across all three lenses.

## 4. New vs reused

| Piece | Status |
| --- | --- |
| `residual_mom_multifactor` / `residual_momentum_sector` | **NEW** (~20 lines, the only real new code) |
| `factors` param in `compute` + `run_backtest` | small additive change |
| Sweep harness, tilt blend, PIT pool, off-nothing-live posture | **REUSE** |

## 5. Constraints honored

- **Inert at `residual_tilt=0.0`** — nothing changes live; adoption is a separate human edit.
- **Never trades.** Pure math + research backtest.
- **Deep-history validated** — survivorship-free pool, multi-regime.
- **No new data** — sector ETFs already cached.

## 6. Testing

- `src/residual.py --selftest` gains multi-factor cases: build 3 factor series and
  names that are (a) a pure factor combination → `residual_mom ≈ 0` (well-conditioned,
  not 0/0 — give a tiny zero-mean idiosyncratic), (b) factor combo + POSITIVE
  idiosyncratic drift → `> 0`, (c) + NEGATIVE → `< 0`; assert signs + ordering; and a
  degenerate case (names with a NaN window → NaN, no raise). Verify numerically before
  handing to the implementer (as with the 1-factor fixture).
- `momentum.compute` regression: `residual_tilt=0.0` still bit-for-bit identical
  (existing selftest); a `factors=` path re-ranks vs `market=` path.

## 7. Risks / watch-items

- **Collinearity** among sector ETFs (and vs the market) — mitigated by `lstsq` (SVD);
  the residual we use is stable; only individual betas would be noisy (we don't use them).
- **12 factors on ~252 obs** — well-conditioned (obs ≫ params); fine.
- **IEX-pool noise** — same caveat as market-only; residual on thin/dead names is only
  as clean as the pool prices.
- **α under multi-factor** — the intercept is the idiosyncratic drift; with a shared,
  full-rank factor set it's estimable, but watch for degeneracy on very short windows
  (guarded by the `valid`/clean-window filter).

## 8. Build order (each its own plan step)

1. `src/residual.py` — add `residual_mom_multifactor` + `residual_momentum_sector` +
   selftest (numerically pre-verified fixture).
2. `momentum.compute` — add `factors=None`; multi-factor path when `residual_tilt>0`
   and `factors` given; `0.0` no-op preserved.
3. `scripts/backtest_pit.py` `run_backtest` — add `factors=None` threaded to the book
   compute; `scripts/backtest_residual.py` — run market-only + sector, print the
   combined table; **RUN it and bring the table back to adjudicate.**

## 9. Future (not this spec)

- Adopt (market-only, sector, or neither) via `[signal] residual_tilt` + a factor-mode
  config, if the table warrants.
- Fama-French style factors (size/value) if sectors also underwhelm.
