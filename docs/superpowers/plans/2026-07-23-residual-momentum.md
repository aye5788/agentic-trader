# Residual Momentum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add residual (market-beta-stripped) momentum as an optional blend in the signal and produce a walk-forward comparison table (CAGR / Sharpe / MaxDD / 2022-unwind) across a `residual_tilt` grid, for a human to adjudicate.

**Architecture:** A pure residual computation (`src/residual.py`) does the regressions; `momentum.compute` gains a `residual_tilt` param that blends the residual rank into the R/σ view (`0.0` = today's signal exactly); a sweep script reuses `backtest_pit`'s survivorship-free engine to run the grid and print the table. One-time validation, not a perpetual dial.

**Tech Stack:** Python, pandas, numpy (all in `.venv`). Repo's inline `--selftest` convention (no pytest). Reuses `scripts/backtest_pit.py` (survivorship-free PIT engine) and `scripts/backtest.py` helpers (`annualized`, `max_drawdown`).

**Spec:** [`docs/superpowers/specs/2026-07-23-residual-momentum-design.md`](../specs/2026-07-23-residual-momentum-design.md)

## Global Constraints

- **Never trades.** Pure signal math + a research backtest. No order path. Nothing changes live until a human sets `residual_tilt` in config.
- **Backward compatible:** `residual_tilt = 0.0` (the default) must reproduce today's `momentum.compute` output **exactly**. Existing callers (`slow_loop`, `backtest_pit`) pass nothing new and are unaffected.
- **Market-only beta first** (regress on SPY). Sector beta is out of scope.
- **Blend on the ranks:** `p_mom = (1−tilt)·rank(ret) + tilt·rank(residual_mom)`, then `score = (p_mom + rank(trend))/2`. `tilt` applies to the **single-name book only**, not the ETF sleeve.
- **No new data.** `pool_closes.parquet` (survivorship-free, has SPY + 11 sector ETFs) and `closes.parquet` already hold everything.
- **Test convention:** inline `def _selftest()` guarded by `if __name__ == "__main__": if "--selftest" in sys.argv: _selftest()`, ending `print("selftest OK: ...")`. `from __future__ import annotations`. Mirror `src/ledger.py`.
- **No look-ahead:** the residual regression uses only data ≤ `asof`, same as `compute`.

---

### Task 1: Pure residual-momentum computation (`src/residual.py`)

The only real new math. A returns-based core (fully testable with clean fixtures) plus a thin price-panel wrapper.

**Files:**
- Create: `src/residual.py`

**Interfaces:**
- Produces:
  - `residual_mom_from_returns(rets: pd.DataFrame, mkt: pd.Series) -> pd.Series` — per-ticker residual momentum from daily returns (tickers = columns of `rets`; `mkt` = market daily returns, same index).
  - `residual_momentum(panel: pd.DataFrame, asof, market: pd.Series, lookback: int = 252) -> pd.Series` — extracts the trailing window from a close-price panel and calls the core.

- [ ] **Step 1: Write the failing selftest**

Create `src/residual.py`:

```python
"""Residual (market-beta-stripped) momentum — pure, no I/O, no clock.

For each ticker over a trailing window, regress its daily returns on the market's
(SPY) and score the idiosyncratic momentum as the regression **intercept α** (the
average non-market return) ÷ residual vol. NOTE: use α, NOT Σresid — OLS residuals
sum to ~0 by construction (with an intercept), so Σresid would be zero for every
name. α is the average idiosyncratic return, which is exactly what we want. Analog
of momentum's `ret = R/σ`, market noise removed. Null-safe: degenerate inputs (no
market variance, empty window) return NaN, never raise.

Spec: docs/superpowers/specs/2026-07-23-residual-momentum-design.md §3.1
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def residual_mom_from_returns(rets: pd.DataFrame, mkt: pd.Series) -> pd.Series:
    """Per-ticker residual momentum from daily returns. rets: dates×tickers; mkt:
    market daily returns (same index). Regress each column on mkt (skipna),
    residual_mom = α / std(resid) — the average idiosyncratic return per unit
    idiosyncratic risk. (NOT Σresid: OLS residuals sum to ~0 with an intercept.)
    NaN where undefined."""
    mkt = mkt.dropna()
    if len(mkt) < 2:
        return pd.Series(index=rets.columns, dtype=float)
    rets = rets.reindex(mkt.index)
    mvar = mkt.var()
    if not mvar or mvar == 0:
        return pd.Series(index=rets.columns, dtype=float)
    dm = mkt - mkt.mean()
    cov = rets.sub(rets.mean()).mul(dm, axis=0).mean()          # per-column cov (skipna)
    beta = cov / mvar
    alpha = rets.mean() - beta * mkt.mean()                     # avg idiosyncratic return
    fitted = pd.DataFrame(np.outer(mkt.values, beta.values),
                          index=mkt.index, columns=rets.columns).add(alpha, axis=1)
    resid = rets.sub(fitted)
    rm = alpha / resid.std()                                    # risk-adjusted idiosyncratic momentum
    return rm.replace([np.inf, -np.inf], np.nan)


def residual_momentum(panel: pd.DataFrame, asof, market: pd.Series,
                      lookback: int = 252) -> pd.Series:
    """Residual momentum per ticker as of `asof`, from a close-price panel and a
    market close-price Series. Empty Series if the window is too short."""
    hist = panel.loc[:asof]
    if len(hist) < lookback + 1:
        return pd.Series(dtype=float)
    win = hist.iloc[-(lookback + 1):]
    rets = win.pct_change().iloc[1:]
    mkt = market.loc[:asof].reindex(win.index).pct_change().iloc[1:]
    return residual_mom_from_returns(rets, mkt)


def _selftest() -> None:
    # Clean fixture: market returns m; three names with known idiosyncratic content.
    m = pd.Series([0.01, -0.02, 0.015, -0.01, 0.02, -0.015, 0.01, -0.005],
                  index=pd.RangeIndex(8))
    d_pos = pd.Series([0.01, 0.008, 0.012, 0.009, 0.011, 0.007, 0.013, 0.010], index=m.index)
    d_zero = pd.Series([0.01, -0.01, 0.01, -0.01, 0.01, -0.01, 0.01, -0.01], index=m.index)
    a = 0.5 * m + d_zero    # half-beta + ZERO-mean idiosyncratic  -> residual_mom ~ 0
    b = 0.5 * m + d_pos     # half-beta + POSITIVE idiosyncratic    -> residual_mom > 0
    c = 0.5 * m - d_pos     # half-beta + NEGATIVE idiosyncratic    -> residual_mom < 0
    rm = residual_mom_from_returns(pd.DataFrame({"A": a, "B": b, "C": c}), m)
    assert rm["B"] > 0, rm.to_dict()                     # positive idiosyncratic drift
    assert rm["C"] < 0, rm.to_dict()                     # negative idiosyncratic drift
    assert rm["B"] > rm["A"] > rm["C"], rm.to_dict()     # correct ordering (A ~ 0 in the middle)
    assert abs(rm["A"]) < 1.0, rm["A"]                   # zero-mean idiosyncratic -> near 0
    # (verified numerically: B≈+6.3, C≈-7.5, A≈-0.08.) NOTE: don't test an EXACT
    # market rider (a=1.0*m) — its residual is ~0 with ~0 variance, so α/std is
    # a 0/0 float-garbage value, not a meaningful assertion.

    # degenerate: zero market variance -> all NaN, no raise
    flat = pd.Series([0.0] * 8, index=m.index)
    assert residual_mom_from_returns(pd.DataFrame({"A": a, "B": b}), flat).isna().all()

    print("selftest OK: residual_mom_from_returns (sign + ordering + degenerate)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it passes**

Run: `.venv/bin/python src/residual.py --selftest`
Expected: `selftest OK: residual_mom_from_returns (sign + ordering + degenerate)`
(The full implementation is written in Step 1, so it passes immediately — this module is transcription with property-based vectors: B/C carry known-sign idiosyncratic drift (B≈+6.3, C≈−7.5), A is zero-mean idiosyncratic (≈0), flat market → all NaN. All verified numerically. If it fails, diff against the plan; don't weaken the assertions.)

- [ ] **Step 3: Commit**

```bash
git add src/residual.py
git commit -m "feat(signal): pure residual (market-beta-stripped) momentum

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Thread `residual_tilt` through `momentum.compute`

Add the blend knob. `residual_tilt = 0.0` must reproduce today's score exactly.

**Files:**
- Modify: `src/momentum.py`

**Interfaces:**
- Consumes: `residual.residual_momentum` (Task 1).
- Produces: `compute(panel, asof, lookback=252, residual_tilt=0.0, market=None)` — unchanged when `residual_tilt=0.0`; when `>0`, blends the residual rank into the R/σ view (requires `market`, a close-price Series).

- [ ] **Step 1: Write the failing test**

Add to `src/momentum.py` a `_selftest()` (the file has none today) — append above any `if __name__`:

```python
def _selftest() -> None:
    import numpy as np
    # 260 business days so lookback=252 has a full window.
    idx = pd.date_range("2024-01-01", periods=260, freq="B")
    rng = np.random.default_rng(0)
    mkt = pd.Series(100 * (1 + pd.Series(rng.normal(0.0005, 0.01, 260), index=idx)).cumprod().values, index=idx)
    panel = pd.DataFrame({"SPY": mkt.values}, index=idx)
    for i in range(6):
        panel[f"T{i}"] = 100 * (1 + pd.Series(rng.normal(0.0008, 0.02, 260), index=idx)).cumprod().values
    asof = idx[-1]

    # residual_tilt=0.0 reproduces the classic score exactly.
    base = compute(panel, asof)                                  # default tilt 0
    d = compute(panel, asof, residual_tilt=0.0, market=panel["SPY"])
    assert base["score"].equals(d["score"]), "tilt=0 must equal the classic score"
    # classic formula check: score == mean(rank(ret), rank(trend))
    expect = (base["ret"].rank(pct=True) + base["trend"].rank(pct=True)) / 2.0
    assert np.allclose(base["score"].values, expect.reindex(base.index).values), "score formula drifted"

    # residual_tilt=1.0 changes the ranking (uses residual instead of R/σ).
    full = compute(panel, asof, residual_tilt=1.0, market=panel["SPY"])
    assert not full["score"].equals(base["score"]), "tilt=1 should re-rank"

    print("selftest OK: compute residual_tilt (0=identity, 1=re-ranks)")
```
Wire it: at the file's bottom add
```python
if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/momentum.py --selftest`
Expected: FAIL — `compute() got an unexpected keyword argument 'residual_tilt'`.

- [ ] **Step 3: Implement the blend**

In `src/momentum.py`, change the `compute` signature and the scoring block. Signature:
```python
def compute(panel: pd.DataFrame, asof: pd.Timestamp,
            lookback: int = LOOKBACK, residual_tilt: float = 0.0,
            market: "pd.Series | None" = None) -> pd.DataFrame:
```
Replace the scoring block (the `p_ret`/`p_trend`/`score` lines) with:
```python
    df["ret"] = df["R"] / df["sigma"]

    p_ret = df["ret"].rank(pct=True)
    p_trend = df["trend"].rank(pct=True)
    if residual_tilt and residual_tilt > 0.0 and market is not None:
        import residual
        rm = residual.residual_momentum(panel, asof, market, lookback).reindex(df.index)
        p_resid = rm.rank(pct=True).fillna(p_ret)     # missing residual -> fall back to R/σ rank
        p_mom = (1.0 - residual_tilt) * p_ret + residual_tilt * p_resid
    else:
        p_mom = p_ret
    df["score"] = (p_mom + p_trend) / 2.0
```
(Leave `eligible`/`rank`/`sort_values` below it unchanged.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/momentum.py --selftest`
Expected: `selftest OK: compute residual_tilt (0=identity, 1=re-ranks)`

- [ ] **Step 5: Commit**

```bash
git add src/momentum.py
git commit -m "feat(signal): residual_tilt blend in compute (0.0 = unchanged)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The comparison backtest (`scripts/backtest_residual.py`) + `run_backtest` param

Sweep `residual_tilt` over the survivorship-free pool and print the table. Reuses `backtest_pit`'s engine; the only change there is threading `residual_tilt` into the book's `compute` call.

**Files:**
- Modify: `scripts/backtest_pit.py` (add `residual_tilt` param to `run_backtest`)
- Create: `scripts/backtest_residual.py`

**Interfaces:**
- Consumes: `backtest_pit._load_pit_data`, `backtest_pit.run_backtest`, `backtest.max_drawdown`.

- [ ] **Step 1: Thread `residual_tilt` into `run_backtest`**

In `scripts/backtest_pit.py`, change `run_backtest`'s signature to add `residual_tilt: float = 0.0` (keep `cap_params=None` last or add after it — match the existing style), and change ONLY the **book** compute call:
```python
        book_scored = mom.compute(closes[univ], t0, residual_tilt=residual_tilt, market=spy)
```
Leave the ETF sleeve call (`etf_scored = mom.compute(etf_panel, t0)`) unchanged. `residual_tilt=0.0` (the default) leaves every existing caller — including `run_sweep` and `main` — behaving exactly as before.

- [ ] **Step 2: Write the sweep script**

Create `scripts/backtest_residual.py`:

```python
"""Residual-momentum comparison — sweep residual_tilt over the survivorship-free
PIT pool and print CAGR / Sharpe / MaxDD / 2022-unwind DD per tilt. One-time
research validation (not a live path, not a cron). Reuses backtest_pit's engine.

    python scripts/backtest_residual.py

Spec: docs/superpowers/specs/2026-07-23-residual-momentum-design.md §3.3
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import backtest as bt              # noqa: E402
import backtest_pit as pit         # noqa: E402

GRID = [0.0, 0.25, 0.5, 0.75, 1.0]


def _dd_2022(equity: pd.Series) -> float:
    """Max drawdown restricted to calendar 2022 (the momentum unwind)."""
    sl = equity[(equity.index >= "2022-01-01") & (equity.index <= "2022-12-31")]
    return bt.max_drawdown(sl) if len(sl) > 1 else float("nan")


def main() -> None:
    data = pit._load_pit_data()   # (closes, dvol, candidates, etfs, spy, etf_panel, rebals, P)
    print(f"\n{'residual_tilt':>13}{'CAGR':>9}{'Sharpe':>8}{'MaxDD':>9}{'2022DD':>9}{'vs w=0':>10}")
    print("-" * 58)
    base_cagr = None
    for w in GRID:
        r = pit.run_backtest(*data, residual_tilt=w)
        eq = r["res"]["equity"]
        dd22 = _dd_2022(eq)
        if base_cagr is None:
            base_cagr = r["cagr"]
        delta = "" if w == 0.0 else f"{(r['cagr'] - base_cagr):+.1%} CAGR"
        print(f"{w:>13.2f}{r['cagr']:>8.1%}{r['sharpe']:>8.2f}{r['maxdd']:>9.1%}{dd22:>9.1%}{delta:>10}")
    print("\nRead all three lenses (return / Sharpe / drawdown). Adjudicate — no auto-adopt.")


if __name__ == "__main__":
    main()
```

Note: `run_backtest(*data, residual_tilt=w)` unpacks the 8-tuple from `_load_pit_data` as the 8 positional args (`closes, dvol, candidates, etfs, spy, etf_panel, rebals, P`); `residual_tilt` is passed by keyword. Confirm `_load_pit_data` returns exactly that 8-tuple before running (it does per `run_sweep`).

- [ ] **Step 3: Compile + run the real comparison**

Run: `python -m py_compile scripts/backtest_pit.py scripts/backtest_residual.py && echo compile-ok`
Expected: `compile-ok`

Then the real run (needs the pool cache `research_store/prices/pool_closes.parquet`, present on the box):
Run: `.venv/bin/python scripts/backtest_residual.py`
Expected: the comparison table — 5 rows (tilt 0…1) with CAGR / Sharpe / MaxDD / 2022DD, `w=0` matching the known baseline (~23% CAGR / 0.97 Sharpe from `backtest_pit`). **Paste the table into the report** — it is the deliverable to adjudicate.

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest_pit.py scripts/backtest_residual.py
git commit -m "feat(signal): residual-momentum comparison sweep over the PIT pool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **You're on the box** — the pool cache exists, so Task 3 Step 3 produces a real table. That table is the point of the whole plan; capture it verbatim.
- **Backward-compat is the load-bearing invariant:** Task 2's selftest locks `residual_tilt=0.0` == today's score. If it ever fails, stop — the default must never change the live signal.
- **Do not set `residual_tilt` anywhere in config** — adoption is a separate human decision *after* reading the table. This plan only makes the comparison possible; it changes nothing live.
- The `import residual` inside `compute` (Task 2 Step 3) is deliberate — it's only reached when `residual_tilt>0`, keeping the import off the hot path for the default case and avoiding a hard dependency for existing callers.
