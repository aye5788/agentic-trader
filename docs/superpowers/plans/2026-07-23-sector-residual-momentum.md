# Sector-Residual Momentum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-factor (11 SPDR sector) residual-momentum variant and extend the comparison sweep to print market-only vs sector across the `residual_tilt` grid.

**Architecture:** A vectorized multivariate-OLS residual (`src/residual.py`, added alongside the merged 1-factor version) regresses each name on the 11 sector-ETF returns; `momentum.compute` gains a `factors=` path (still `residual_tilt=0.0` = no-op); the sweep runs both variants. One-time validation, inert until a human adopts.

**Tech Stack:** Python, numpy (`lstsq`), pandas. Repo inline `--selftest` (no pytest). Reuses `scripts/backtest_pit.py` + `scripts/backtest.py` helpers, and the merged residual-momentum machinery.

**Spec:** [`docs/superpowers/specs/2026-07-23-sector-residual-momentum-design.md`](../specs/2026-07-23-sector-residual-momentum-design.md)

## Global Constraints

- **Never trades / never gates.** Pure math + a research backtest. No order path.
- **Inert at default:** `residual_tilt=0.0` reproduces today's signal bit-for-bit; the `factors=` path is only reached when `residual_tilt>0`. Existing callers unaffected.
- **The signal is `α / std(resid)`** (regression intercept ÷ residual vol) — same as the 1-factor version, now multivariate. NOT Σresid.
- **Factors = the 11 SPDR sector ETFs** (`XLE, XLF, XLK, XLV, XLI, XLP, XLY, XLU, XLB, XLRE, XLC`), NO separate SPY. No per-name sector map.
- **Collinearity → `np.linalg.lstsq` (SVD)**: the residual is stable; we never use individual betas.
- **NaN handling:** solve only names with a complete non-NaN window; others → NaN (blend falls back to `p_ret`).
- **No look-ahead:** window ends at `asof`.
- **Test convention:** inline `--selftest`, mirror `src/ledger.py`; `from __future__ import annotations`.

---

### Task 1: Multi-factor residual computation (`src/residual.py`)

Add two functions + extend the selftest. Do NOT change the existing 1-factor functions.

**Files:**
- Modify: `src/residual.py`

**Interfaces:**
- Produces:
  - `residual_mom_multifactor(rets: pd.DataFrame, factor_rets: pd.DataFrame) -> pd.Series`
  - `residual_momentum_sector(panel, asof, factor_panel: pd.DataFrame, lookback: int = 252) -> pd.Series`

- [ ] **Step 1: Add the two functions**

Append to `src/residual.py` (imports `numpy as np`, `pandas as pd` already present):

```python
def residual_mom_multifactor(rets, factor_rets):
    """Multivariate residual momentum: regress each name's returns on the factor
    returns (intercept + factors) via one lstsq, residual_mom = α / std(resid).
    Solves only names with a complete non-NaN window; others -> NaN. NaN-safe."""
    fr = factor_rets.dropna()
    if len(fr) < len(factor_rets.columns) + 2:            # need obs > params
        return pd.Series(index=rets.columns, dtype=float)
    rets = rets.reindex(fr.index)
    X = np.column_stack([np.ones(len(fr)), fr.values])   # T x (1+K)
    clean = ~rets.isna().any()
    out = pd.Series(np.nan, index=rets.columns)
    if not clean.any():
        return out
    Yc = rets.loc[:, clean].values                       # T x Nc
    B, *_ = np.linalg.lstsq(X, Yc, rcond=None)           # (1+K) x Nc
    resid = Yc - X @ B
    alpha = B[0]
    std = resid.std(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        rm = np.where(std > 0, alpha / std, np.nan)
    out.loc[clean] = rm
    return out.replace([np.inf, -np.inf], np.nan)


def residual_momentum_sector(panel, asof, factor_panel, lookback: int = 252):
    """Multi-factor residual momentum per ticker as of `asof`, from a close-price
    panel and a factor close-price panel (the sector ETFs). Empty if window short."""
    hist = panel.loc[:asof]
    if len(hist) < lookback + 1:
        return pd.Series(dtype=float)
    win = hist.iloc[-(lookback + 1):]
    rets = win.pct_change().iloc[1:]
    frets = factor_panel.loc[:asof].reindex(win.index).pct_change().iloc[1:]
    return residual_mom_multifactor(rets, frets)
```

- [ ] **Step 2: Extend the selftest**

In `src/residual.py`'s `_selftest()`, BEFORE the final `print("selftest OK: ...")`, add the multi-factor block (verified numerically: PURE≈0.06, POS≈+0.69, NEG≈−0.69):

```python
    # --- multi-factor (sector) residual ---
    nn = 60
    tt = np.arange(nn)
    S = pd.DataFrame({
        "S1": 0.001 + 0.012 * np.sin(2 * np.pi * tt / 15),
        "S2": 0.0005 + 0.010 * np.sin(2 * np.pi * tt / 11 + 0.7),
        "S3": -0.0003 + 0.008 * np.sin(2 * np.pi * tt / 9 + 1.3),
    }, index=pd.RangeIndex(nn))
    dp = pd.Series(0.0018 + 0.004 * np.sin(2 * np.pi * tt / 7), index=S.index)   # +mean idio
    dz = pd.Series(0.004 * np.sin(2 * np.pi * tt / 8 + 0.4), index=S.index)      # 0-mean idio
    mf = pd.DataFrame({
        "PURE": 0.8 * S["S1"] + 0.5 * S["S2"] + dz,     # spanned by factors -> ~0 residual mom
        "POS":  0.8 * S["S1"] + 0.3 * S["S2"] + dp,     # + positive idiosyncratic
        "NEG":  0.8 * S["S1"] + 0.3 * S["S2"] - dp,     # + negative idiosyncratic
    })
    rmf = residual_mom_multifactor(mf, S)
    assert rmf["POS"] > 0 and rmf["NEG"] < 0, rmf.round(3).to_dict()
    assert rmf["POS"] > rmf["PURE"] > rmf["NEG"], rmf.round(3).to_dict()
    assert abs(rmf["PURE"]) < 1.0, rmf["PURE"]
    mf_nan = mf.copy(); mf_nan.iloc[5, 0] = np.nan
    assert pd.isna(residual_mom_multifactor(mf_nan, S)["PURE"]), "NaN-window name -> NaN"
```
Then change the final print to `print("selftest OK: residual (single + multi-factor)")`.

- [ ] **Step 3: Run to verify it passes**

Run: `.venv/bin/python src/residual.py --selftest`
Expected: `selftest OK: residual (single + multi-factor)`
(Numerically pre-verified: POS≈+0.69, NEG≈−0.69, PURE≈+0.06, NaN-window → NaN. If it fails, diff against the plan; don't weaken assertions.)

- [ ] **Step 4: Commit**

```bash
git add src/residual.py
git commit -m "feat(signal): multi-factor (sector) residual momentum via lstsq

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `factors=` path in `momentum.compute`

Route the residual blend to the multi-factor computation when a `factors` panel is given. `residual_tilt=0.0` stays the verified no-op.

**Files:**
- Modify: `src/momentum.py`

**Interfaces:**
- Consumes: `residual.residual_momentum_sector` (Task 1), `residual.residual_momentum` (existing).
- Produces: `compute(panel, asof, lookback=252, residual_tilt=0.0, market=None, factors=None)`.

- [ ] **Step 1: Add the failing test**

In `src/momentum.py`'s `_selftest()`, BEFORE the final `print(...)`, add (this ties the compute factors-path to Task 1's verified math):

```python
    # --- sector (multi-factor) path ---
    import residual as _res
    ns = 260
    sidx = pd.date_range("2024-01-01", periods=ns, freq="B")
    st = np.arange(ns)
    def _p(r): return pd.Series(100 * np.cumprod(1 + r), index=sidx)
    S1 = 0.0009 + 0.012 * np.sin(2 * np.pi * st / 15)
    S2 = 0.0006 + 0.010 * np.sin(2 * np.pi * st / 11 + 0.7)
    factors = pd.DataFrame({"XLK": _p(S1), "XLF": _p(S2)}, index=sidx)
    dps = 0.0018 + 0.004 * np.sin(2 * np.pi * st / 7)
    spanel = pd.DataFrame({
        "SPANNED": _p(0.8 * S1 + 0.5 * S2 + 0.004 * np.sin(2 * np.pi * st / 8 + 0.4)),  # ~0 sector-residual
        "IDIO":    _p(0.4 * S1 + dps),                                                  # strong sector-residual
        "G1": _p(1.0 * S1 + 0.002 * np.sin(2 * np.pi * st / 6)),
        "G2": _p(0.7 * S2 - 0.0015 * np.sin(2 * np.pi * st / 9)),
        "G3": _p(0.5 * S1 + 0.5 * S2 + 0.002 * np.sin(2 * np.pi * st / 13)),  # zero-mean idio (NOT a constant — a constant gives ~0 residual var -> blowup)
    }, index=sidx)
    sasof = sidx[-1]
    # tilt=0 is still identity on this panel
    assert compute(spanel, sasof)["score"].equals(
        compute(spanel, sasof, residual_tilt=0.0, factors=factors)["score"]), "tilt0 identity (sector)"
    # the sector residual is well-conditioned (ties to Task 1 math): IDIO >> SPANNED
    rms = _res.residual_momentum_sector(spanel, sasof, factors)
    assert rms["IDIO"] > rms["SPANNED"], rms.round(3).to_dict()
    # the factors path produces finite scores and re-ranks vs baseline
    fs = compute(spanel, sasof, residual_tilt=1.0, factors=factors)
    assert fs["score"].notna().all() and not compute(spanel, sasof)["score"].equals(fs["score"]), "sector path re-ranks"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/momentum.py --selftest`
Expected: FAIL — `compute() got an unexpected keyword argument 'factors'`.

- [ ] **Step 3: Implement the `factors` path**

In `src/momentum.py`, add `factors` to the signature:
```python
def compute(panel: pd.DataFrame, asof: pd.Timestamp,
            lookback: int = LOOKBACK, residual_tilt: float = 0.0,
            market: "pd.Series | None" = None,
            factors: "pd.DataFrame | None" = None) -> pd.DataFrame:
```
Change the residual-path condition (currently `if residual_tilt and residual_tilt > 0.0 and market is not None:`) to route to the multi-factor computation when `factors` is given:
```python
    if residual_tilt and residual_tilt > 0.0 and (factors is not None or market is not None):
        import residual
        if factors is not None:
            rm = residual.residual_momentum_sector(panel, asof, factors, lookback).reindex(df.index)
        else:
            rm = residual.residual_momentum(panel, asof, market, lookback).reindex(df.index)
        p_resid = rm.rank(pct=True).fillna(p_ret)
        p_mom = (1.0 - residual_tilt) * p_ret + residual_tilt * p_resid
    else:
        p_mom = p_ret
```
(Leave `df["score"] = (p_mom + p_trend) / 2.0` and everything below unchanged.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/momentum.py --selftest`
Expected: exits 0, prints the existing `selftest OK: ...` line, no `AssertionError`.

- [ ] **Step 5: Commit**

```bash
git add src/momentum.py
git commit -m "feat(signal): factors= (sector) path in compute (0.0 still no-op)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Extend the sweep to compare both variants (+ run it)

**Files:**
- Modify: `scripts/backtest_pit.py` (add `factors=None` to `run_backtest`), `scripts/backtest_residual.py` (both variants + combined table)

**Interfaces:**
- Consumes: `backtest_pit._load_pit_data`, `backtest_pit.run_backtest`, `backtest.max_drawdown`.

- [ ] **Step 1: Thread `factors` into `run_backtest`**

In `scripts/backtest_pit.py`, add `factors=None` to `run_backtest`'s signature (after `residual_tilt`), and change ONLY the book compute call:
```python
        book_scored = mom.compute(closes[univ], t0, residual_tilt=residual_tilt, market=spy, factors=factors)
```
(ETF sleeve call unchanged. `factors=None` default → existing callers, incl. the market-only sweep, unaffected: with `factors=None` compute uses the `market=spy` path exactly as today.)

- [ ] **Step 2: Extend the sweep script**

In `scripts/backtest_residual.py`, replace `main()` so it runs both variants. Build the sector-ETF close panel from the pool `closes` and pass it as `factors` for the sector rows:

```python
SECTORS = ["XLE", "XLF", "XLK", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC"]


def _row(r, base_cagr):
    eq = r["res"]["equity"]
    delta = "" if base_cagr is None else f"{(r['cagr'] - base_cagr):+.1f}"
    return f"{r['cagr']*100:>6.1f}{r['sharpe']:>7.2f}{r['maxdd']*100:>7.1f}{_dd_2022(eq)*100:>7.1f}"


def main() -> None:
    data = pit._load_pit_data()   # (closes, dvol, candidates, etfs, spy, etf_panel, rebals, P)
    closes = data[0]
    sect = [s for s in SECTORS if s in closes.columns]
    factor_panel = closes[sect]
    print(f"  using {len(sect)}/11 sector ETFs: {sect}")
    hdr = f"{'tilt':>5} | {'MARKET-ONLY  CAGR  Shrp  MaxDD 2022':>34} | {'SECTOR      CAGR  Shrp  MaxDD 2022':>34}"
    print("\n" + hdr); print("-" * len(hdr))
    base_m = None
    for w in GRID:
        rm_ = pit.run_backtest(*data, residual_tilt=w)                    # market-only
        rs_ = pit.run_backtest(*data, residual_tilt=w, factors=factor_panel)  # sector
        if base_m is None:
            base_m = rm_["cagr"]
        print(f"{w:>5.2f} | {'':>6}{_row(rm_, None)} | {'':>6}{_row(rs_, None)}")
    print("\nRead all three lenses per variant (return / Sharpe / drawdown). Adjudicate — no auto-adopt.")
```
(Keep the existing `GRID` and `_dd_2022` helper; `import` lines unchanged.)

- [ ] **Step 3: Compile + RUN the real comparison**

Run: `python -m py_compile scripts/backtest_pit.py scripts/backtest_residual.py && echo compile-ok`
Expected: `compile-ok`

Then (the pool cache is on the box; this runs ~10 full backtests, a few minutes):
Run: `.venv/bin/python scripts/backtest_residual.py`
Expected: a table with both MARKET-ONLY and SECTOR columns across tilt 0…1; market-only `w=0` matches the known baseline (~21.6% CAGR / 0.90 Sharpe). **Paste the FULL table into the report — it is the deliverable.**

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest_pit.py scripts/backtest_residual.py
git commit -m "feat(signal): sector-vs-market residual comparison sweep

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **You're on the box; the pool cache exists** — Task 3 produces a real table. That table is the point — capture it verbatim.
- **Backward-compat is load-bearing:** `residual_tilt=0.0` (and `factors=None`) must never change the live signal. The Task 2 selftest locks tilt=0 identity.
- **Do not set `residual_tilt` or `factors` in any config** — adoption is a separate human decision after reading the table.
- Verified before writing: the multi-factor core gives POS≈+0.69 / NEG≈−0.69 / PURE≈+0.06 and NaN-window→NaN.
