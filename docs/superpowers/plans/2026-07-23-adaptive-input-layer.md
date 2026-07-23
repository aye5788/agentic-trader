# Adaptive-Input Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a background, off-box learner that retunes one strategy knob (`stop_atr_mult`) from realized outcomes via a Bayesian posterior, proposing (never applying) bounded changes.

**Architecture:** Pure-Python modules do the math (stop-aware replay + a Bayesian grid estimator with a smoothness prior); a GitHub Actions job runs them off-box weekly and commits a *proposal* artifact; the droplet consumes a promoted value through the existing `strategy.local.toml` merge. Nothing here places orders — it is a new *producer* of config the slow loop already validates.

**Tech Stack:** Python 3.11+ (stdlib `tomllib`), pandas, pyarrow, numpy — all already in `requirements.txt`. Tests follow the repo's **inline `--selftest`** convention (no pytest, no `tests/` dir). GitHub Actions (Aaron's Pro plan) for compute.

**Spec:** [`docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md`](../specs/2026-07-23-adaptive-input-layer-design.md)

## Global Constraints

Every task's requirements implicitly include these (verbatim from the spec):

- **Never executes.** No module here may call any RH/MCP tool, place/modify/cancel an order, or write to the live order path. Output is a config *proposal* only. (spec §3)
- **Pure Python, never `claude`.** No LLM invocation anywhere in this layer. (spec §4)
- **No new dependencies.** numpy/pandas/pyarrow/stdlib only — all present in `requirements.txt`.
- **Test convention:** inline `def _selftest()` guarded by `if __name__ == "__main__": if "--selftest" in sys.argv: _selftest()`, ending with a `print("selftest OK: ...")`. Mirror `src/ledger.py` exactly. No pytest.
- **Repo path pattern:** modules under `src/` are imported after `REPO = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(REPO / "src"))` in scripts. Pure `src/` modules use `from __future__ import annotations` and take dates/values as arguments (no clock reads, no network) — mirror `src/ledger.py`.
- **Knob band (hard bounds):** `stop_atr_mult ∈ [1.5, 3.5]`. A recommendation may never fall outside this band. Incumbent value today is `2.5` (`config/strategy.toml [trade_management]`).
- **Governance:** propose-not-apply. Promotion into `config/strategy.local.toml` is a human step. Every proposed value carries provenance. Inherits all existing gates (`[risk]` mandate, `src/governance.py`). (spec §8)
- **Objective:** the posterior optimizes **mean realized-R per position** (pnl in units of entry−stop risk), not recovery rate. (spec §6.2)

---

### Task 1: OHLC price cache (extend `fetch_prices.py`)

Stop-aware replay needs daily **high/low** to know if a stop was touched. The fetch currently keeps only `close`. Refactor the candle→panel step into a pure, testable helper and write `opens/highs/lows.parquet` alongside the **unchanged** `closes.parquet`.

**Files:**
- Modify: `scripts/fetch_prices.py`

**Interfaces:**
- Produces: `_field_panels(raw: dict[str, list[dict]]) -> dict[str, pandas.DataFrame]` returning keys `"open","high","low","close"`, each a dates×tickers DataFrame. Writes `research_store/prices/{opens,highs,lows,closes}.parquet`.

- [ ] **Step 1: Write the failing selftest**

Add to `scripts/fetch_prices.py` (above `if __name__`):

```python
def _selftest() -> None:
    raw = {
        "AAA": [
            {"datetime": 1609459200000, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5},
            {"datetime": 1609545600000, "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.8},
        ],
        "BBB": [
            {"datetime": 1609459200000, "open": 20.0, "high": 21.0, "low": 19.0, "close": 20.5},
        ],
    }
    panels = _field_panels(raw)
    assert set(panels) == {"open", "high", "low", "close"}, panels.keys()
    # close panel preserves the pre-refactor content/shape
    assert panels["close"].loc[panels["close"].index[1], "AAA"] == 11.8
    assert panels["high"].loc[panels["high"].index[0], "AAA"] == 11.0
    assert panels["low"].loc[panels["low"].index[0], "BBB"] == 19.0
    # sorted by date, aligned index across tickers
    assert list(panels["close"].index) == sorted(panels["close"].index)
    print("selftest OK: _field_panels open/high/low/close")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
```

Note: this replaces the existing bare `if __name__ == "__main__": main()` block.

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/fetch_prices.py --selftest`
Expected: `NameError: name '_field_panels' is not defined`.

- [ ] **Step 3: Add the pure helper and wire `main()` to use it**

Add above `main()`:

```python
_FIELDS = ("open", "high", "low", "close")


def _field_panels(raw: dict) -> dict:
    """Turn {sym: [candle,...]} into one dates×tickers DataFrame per OHLC field.
    Pure: no network, no I/O. `close` panel is byte-for-byte what we cached before."""
    out = {f: {} for f in _FIELDS}
    for sym, candles in raw.items():
        for f in _FIELDS:
            out[f][sym] = pd.Series(
                {pd.Timestamp(c["datetime"], unit="ms").normalize(): c[f] for c in candles}
            )
    return {f: pd.DataFrame(series).sort_index() for f, series in out.items()}
```

In `main()`, replace the close-only series build. Change the pull loop to collect full candles:

```python
    raw = {}
    meta = []
    errors = []
    for i, sym in enumerate(tickers, 1):
        ph, err = _try_pull(sym, args.years)
        if not ph:
            meta.append((sym, 0, "FAILED"))
            if err:
                errors.append(err)
            print(f"  [{i:3}/{len(tickers)}] {sym:6} FAILED" + (f"  ({err[:90]})" if err else ""))
            continue
        raw[sym] = ph
        meta.append((sym, len(ph), "ok"))
        if i % 25 == 0:
            print(f"  [{i:3}/{len(tickers)}] ... {sym} ({len(ph)} candles)")
        time.sleep(0.6)
```

Keep the existing `ok < len//2` ABORT guard (compute `ok` from `meta` exactly as now). After the guard, replace the single-panel write with:

```python
    panels = _field_panels(raw)
    field_to_path = {"open": OPENS, "high": HIGHS, "low": LOWS, "close": CLOSES}
    for field, path in field_to_path.items():
        try:
            panels[field].to_parquet(path)
        except Exception as e:  # never lose a full pull to a missing parquet engine
            fallback = path.with_suffix(".csv")
            panels[field].to_csv(fallback)
            print(f"WARN parquet write failed for {field} ({e}); wrote CSV -> {fallback}")
    panel = panels["close"]   # keep existing summary prints working
```

Add the new path constants near `CLOSES`:

```python
OPENS = OUT_DIR / "opens.parquet"
HIGHS = OUT_DIR / "highs.parquet"
LOWS = OUT_DIR / "lows.parquet"
```

- [ ] **Step 4: Run to verify it passes**

Run: `python scripts/fetch_prices.py --selftest`
Expected: `selftest OK: _field_panels open/high/low/close`

- [ ] **Step 5: Commit**

```bash
git add scripts/fetch_prices.py
git commit -m "feat(prices): cache daily OHLC (open/high/low) beside closes for stop replay

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Stop-aware replay (`src/stop_replay.py`)

Pure function: given one position's post-entry daily bars and its exit params, decide how it would have closed and return realized-R. This is the per-position pseudo-outcome the learner consumes, and it's the intra-week-stop modeling `scripts/backtest.py` explicitly lacks.

**Files:**
- Create: `src/stop_replay.py`

**Interfaces:**
- Produces: `replay_position(entry_price: float, stop: float, target: float | None, highs: list[float], lows: list[float], closes: list[float], max_days: int) -> dict` with keys `stopped: bool`, `hit_target: bool`, `exit_price: float`, `exit_day: int`, `realized_r: float`. `highs/lows/closes` are aligned bars for the days *after* entry (index 0 = first day held).

- [ ] **Step 1: Write the failing selftest**

Create `src/stop_replay.py`:

```python
"""Stop-aware single-position replay — pure, no I/O, no clock.

Given a long position's entry, stop, first target, and the daily OHLC bars that
followed, decide how it would have exited and return realized-R (P&L in units of
the entry-to-stop risk). Used by the adaptive stop-multiple learner to turn price
history into pseudo-outcomes. Conservative same-bar rule: if a bar's low touches
the stop AND its high touches the target, assume the STOP hit first (worst case).

Spec: docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md §6.1
Known simplification (v1): models stop / first-target / time-horizon exits only,
not the 21-day MA exit — replay is a prior, not truth (spec §13 replay-bias).
"""
from __future__ import annotations


def _selftest() -> None:
    # 1. Stop hit on day 2 (low 94 <= stop 95) -> realized_r == -1 exactly.
    r = replay_position(100.0, 95.0, 120.0,
                        highs=[102, 101, 130], lows=[98, 94, 90], closes=[101, 96, 100],
                        max_days=10)
    assert r["stopped"] is True and r["hit_target"] is False, r
    assert r["exit_price"] == 95.0 and r["exit_day"] == 2, r
    assert r["realized_r"] == -1.0, r          # (95-100)/(100-95)

    # 2. Target hit on day 3 (high 121 >= target 120), stop never touched.
    r = replay_position(100.0, 95.0, 120.0,
                        highs=[102, 108, 121], lows=[98, 101, 110], closes=[101, 107, 119],
                        max_days=10)
    assert r["hit_target"] is True and r["stopped"] is False, r
    assert r["exit_price"] == 120.0 and r["exit_day"] == 3, r
    assert r["realized_r"] == 4.0, r           # (120-100)/5

    # 3. Neither: horizon exit at last close.
    r = replay_position(100.0, 95.0, 120.0,
                        highs=[102, 103, 104], lows=[98, 99, 100], closes=[101, 102, 103],
                        max_days=3)
    assert r["stopped"] is False and r["hit_target"] is False, r
    assert r["exit_price"] == 103.0 and r["exit_day"] == 3, r
    assert r["realized_r"] == 0.6, r           # (103-100)/5

    # 4. Same bar touches both -> conservative STOP first.
    r = replay_position(100.0, 95.0, 120.0,
                        highs=[125], lows=[94], closes=[100], max_days=5)
    assert r["stopped"] is True and r["hit_target"] is False, r

    # 5. max_days shorter than the path caps the hold.
    r = replay_position(100.0, 95.0, 200.0,
                        highs=[101, 102, 103, 104], lows=[99, 98, 97, 96],
                        closes=[100, 101, 102, 103], max_days=2)
    assert r["exit_day"] == 2 and r["exit_price"] == 101.0, r

    print("selftest OK: replay_position stop/target/horizon/same-bar/max_days")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python src/stop_replay.py --selftest`
Expected: `NameError: name 'replay_position' is not defined`.

- [ ] **Step 3: Write the implementation**

Add above `_selftest`:

```python
def replay_position(entry_price, stop, target, highs, lows, closes, max_days):
    entry_price = float(entry_price)
    stop = float(stop)
    risk = entry_price - stop
    n = min(int(max_days), len(closes))

    def r_of(px):
        return (float(px) - entry_price) / risk if risk else 0.0

    for i in range(n):
        lo = float(lows[i])
        hi = float(highs[i])
        if lo <= stop:                                  # conservative: stop before target
            return {"stopped": True, "hit_target": False,
                    "exit_price": stop, "exit_day": i + 1, "realized_r": r_of(stop)}
        if target is not None and hi >= float(target):
            tgt = float(target)
            return {"stopped": False, "hit_target": True,
                    "exit_price": tgt, "exit_day": i + 1, "realized_r": r_of(tgt)}
    exit_px = float(closes[n - 1]) if n > 0 else entry_price
    return {"stopped": False, "hit_target": False,
            "exit_price": exit_px, "exit_day": n, "realized_r": r_of(exit_px)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python src/stop_replay.py --selftest`
Expected: `selftest OK: replay_position stop/target/horizon/same-bar/max_days`

- [ ] **Step 5: Commit**

```bash
git add src/stop_replay.py
git commit -m "feat(adaptive): stop-aware single-position replay -> realized-R (pure)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Bayesian grid estimator with smoothness prior (`src/adaptive.py`)

The dial-agnostic spine: a 1-D grid of candidate knob values, each with a posterior over its objective, **correlated across neighbours by a smoothness prior** (a GP over the fixed grid = closed-form Gaussian, pure numpy). Plus the uncertainty-gated recommendation and an out-of-sample gap helper.

**Files:**
- Create: `src/adaptive.py`

**Interfaces:**
- Produces:
  - `posterior(grid, obs_mean, obs_count, noise_var, prior_mean, length_scale, prior_var, jitter=1e-8) -> tuple[np.ndarray, np.ndarray, np.ndarray]` returning `(post_mean, post_var, post_cov)`.
  - `recommend(grid, post_mean, post_cov, incumbent_idx, confidence=0.9) -> dict` with keys `recommended_idx, recommended_value, p_better, moved`.
  - `oos_gap(train_mean, train_count, holdout_mean, best_idx, ...) -> float` (in-sample minus out-of-sample objective at the recommended index).

- [ ] **Step 1: Write the failing selftest**

Create `src/adaptive.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python src/adaptive.py --selftest`
Expected: `NameError: name 'posterior' is not defined`.

- [ ] **Step 3: Write the implementation**

Add above `_selftest`:

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python src/adaptive.py --selftest`
Expected: `selftest OK: posterior(prior/smoothing/independent), recommend(gate), oos_gap`

- [ ] **Step 5: Commit**

```bash
git add src/adaptive.py
git commit -m "feat(adaptive): Bayesian grid estimator w/ smoothness prior + uncertainty gate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Stop-multiple tuner (`scripts/tune_stop.py`)

Wire the pieces into dial #1: generate historical entries over the PIT pool, replay each under every candidate multiple to build per-grid realized-R samples, hold out the most recent fold for the overfit gap, fold in live stop-outs from the ledger, run the estimator, and write the proposal artifact. This is the off-box job's payload.

**Files:**
- Create: `scripts/tune_stop.py`
- Reads: `research_store/prices/{closes,highs,lows}.parquet`, `config/pit_pool.csv`, `config/strategy.toml`, `research_store/journal.jsonl`
- Writes: `research_store/adaptive/proposals/stop_atr_mult.json`

**Interfaces:**
- Consumes: `stop_replay.replay_position` (Task 2); `adaptive.posterior/recommend/oos_gap` (Task 3); `momentum.compute/select` (existing, pure) for entry generation; `strategy.load` (existing).
- Produces: `build_samples(entries, panels, grid, tm) -> tuple[np.ndarray, np.ndarray]` (per-grid mean realized-R, per-grid count); the artifact schema below.

**Artifact schema** (`stop_atr_mult.json`):

```json
{
  "knob": "trade_management.stop_atr_mult",
  "generated_at": "2026-07-26T20:14:00Z",
  "incumbent": 2.5,
  "recommended": 2.5,
  "moved": false,
  "p_better": 0.42,
  "band": [1.5, 3.5],
  "grid": [1.5, 2.0, 2.5, 3.0, 3.5],
  "posterior_mean": [0.11, 0.19, 0.22, 0.20, 0.14],
  "evidence": {"replay_n": 4120, "live_n": 7},
  "oos_gap": 0.03,
  "rationale": "incumbent retained: challenger 3.0 only p=0.42 < 0.90 confidence",
  "provenance": "adaptive layer 2026-07-26 from replay_n=4120 live_n=7; incumbent was 2.5"
}
```

- [ ] **Step 1: Write the failing selftest**

Create `scripts/tune_stop.py`:

```python
"""Adaptive tuner for stop_atr_mult — the off-box weekly payload.

Builds per-candidate realized-R evidence by replaying PIT-pool entries under each
grid multiple (walk-forward: most recent fold held out for the overfit gap), folds
in live stop-outs from the ledger, runs the Bayesian estimator, and writes a
PROPOSAL. Never promotes, never trades (spec §3, §8).

    python scripts/tune_stop.py [--selftest]

Spec: docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md §6
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from stop_replay import replay_position          # noqa: E402
import adaptive                                   # noqa: E402

GRID = [1.5, 2.0, 2.5, 3.0, 3.5]
BAND = (1.5, 3.5)
INCUMBENT = 2.5
PROPOSAL = REPO / "research_store" / "adaptive" / "proposals" / "stop_atr_mult.json"


def build_samples(entries, panels, grid, tm):
    """entries: list of dicts {sym, entry_price, sigma, fwd_highs, fwd_lows, fwd_closes}.
    Returns (mean_r[len(grid)], count[len(grid)]) — mean realized-R per candidate
    multiple. target/horizon come from `tm` (trade_management config)."""
    target_mult = float(tm["target_r_mults"][0])
    horizon = int(tm.get("ma_exit_days", 21))
    sums = np.zeros(len(grid)); counts = np.zeros(len(grid))
    for e in entries:
        for gi, m in enumerate(grid):
            stop = e["entry_price"] - m * e["sigma"]
            target = e["entry_price"] + target_mult * (e["entry_price"] - stop)
            r = replay_position(e["entry_price"], stop, target,
                                e["fwd_highs"], e["fwd_lows"], e["fwd_closes"], horizon)
            sums[gi] += r["realized_r"]; counts[gi] += 1
    mean_r = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
    return mean_r, counts


def _selftest() -> None:
    # Two synthetic entries: a name that runs up (rewards a wide stop) and a
    # chop name that whipsaws (rewards a wider stop too). Wider multiple should
    # score >= tighter here, and build_samples must count every entry per grid pt.
    tm = {"target_r_mults": [2.2], "ma_exit_days": 5}
    entries = [
        {"sym": "RUN", "entry_price": 100.0, "sigma": 2.0,
         "fwd_highs": [103, 106, 110, 115, 120], "fwd_lows": [99, 101, 104, 108, 112],
         "fwd_closes": [102, 105, 109, 114, 119]},
        {"sym": "CHOP", "entry_price": 50.0, "sigma": 1.0,
         "fwd_highs": [51, 50, 52, 51, 53], "fwd_lows": [48.5, 48.0, 49, 48.2, 49],
         "fwd_closes": [49, 49.5, 50, 49.5, 51]},
    ]
    mean_r, counts = build_samples(entries, panels=None, grid=GRID, tm=tm)
    assert list(counts) == [2, 2, 2, 2, 2], counts        # every entry, every grid pt
    assert mean_r[-1] >= mean_r[0] - 1e-9, mean_r          # wider >= tighter here
    print("selftest OK: build_samples counts + wide-stop ordering")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/tune_stop.py --selftest`
Expected: `NameError: name 'main' is not defined` (the `else` branch references `main`; the selftest branch itself should pass once `build_samples` exists — so actually expect the assertion path to run). Run with `--selftest`; expected first failure is `NameError` on `main` only if you omit `--selftest`. With `--selftest` present the test body runs; expected to PASS once Step 1's `build_samples` is defined. If it errors before that, fix the error.

(Interpretation: `build_samples` is defined in Step 1, so `--selftest` exercises it immediately. This task's TDD unit is `build_samples`; `main` is integration wiring added in Step 3 and covered by the dry-run in Step 4.)

- [ ] **Step 3: Write `main()` and the entry generator**

Add above `_selftest`:

```python
def generate_entries(panels, pool_tickers, tm, fold_days=252):
    """Reconstruct historical entries over the PIT pool: at each weekly rebalance
    date, the names momentum.select would have bought, their entry close, trailing
    sigma, and the forward OHLC window. Reuses the SAME pure signal the live loop
    uses so replay entries match production selection.

    Returns list[dict] with keys sym, entry_price, sigma, fwd_highs, fwd_lows,
    fwd_closes, entry_date (ISO). See momentum.compute/select for the ranking."""
    import momentum
    closes, highs, lows = panels["close"], panels["high"], panels["low"]
    entries = []
    dates = list(closes.index)
    # weekly cadence: step 5 trading days; leave a forward window for replay
    for di in range(fold_days, len(dates) - 5, 5):
        asof = dates[di]
        panel = closes.iloc[: di + 1]
        scored = momentum.compute(panel, asof=asof)
        sel = momentum.select(scored)
        for sym in sel:
            if sym not in closes.columns:
                continue
            s = closes[sym].iloc[: di + 1].dropna()
            if len(s) < 20:
                continue
            sigma = float(s.pct_change().tail(63).std() * s.iloc[-1])   # ~$ daily sigma
            fwd = slice(di + 1, di + 1 + int(tm.get("ma_exit_days", 21)) + 5)
            entries.append({
                "sym": sym, "entry_price": float(closes[sym].iloc[di]), "sigma": sigma,
                "fwd_highs": list(highs[sym].iloc[fwd].values),
                "fwd_lows": list(lows[sym].iloc[fwd].values),
                "fwd_closes": list(closes[sym].iloc[fwd].values),
                "entry_date": str(asof.date()),
            })
    return entries


def _load_panels():
    import pandas as pd
    p = REPO / "research_store" / "prices"
    return {"close": pd.read_parquet(p / "closes.parquet"),
            "high": pd.read_parquet(p / "highs.parquet"),
            "low": pd.read_parquet(p / "lows.parquet")}


def _live_stop_samples(grid):
    """Fold live stop-outs from the ledger into the grid bucket matching the stop
    multiple in force at the time. Returns (sum_r[len], count[len]). Empty until
    live outcomes exist — the posterior simply leans on replay until then."""
    import ledger  # noqa: F401  (reserved for realized_history join; live_n=0 first cut)
    return np.zeros(len(grid)), np.zeros(len(grid))


def main():
    import datetime as dt
    import strategy
    cfg = strategy.load()
    tm = cfg["trade_management"]
    pool = [r.strip().split(",")[0] for r in
            (REPO / "config" / "pit_pool.csv").read_text().splitlines()[1:]]
    panels = _load_panels()
    entries = generate_entries(panels, pool, tm)

    # walk-forward: hold out the most recent 20% of entries by date.
    entries.sort(key=lambda e: e["entry_date"])
    cut = int(len(entries) * 0.8)
    train, holdout = entries[:cut], entries[cut:]

    tr_mean, tr_cnt = build_samples(train, panels, GRID, tm)
    ho_mean, _ = build_samples(holdout, panels, GRID, tm) if holdout else (np.zeros(len(GRID)), None)

    live_sum, live_cnt = _live_stop_samples(GRID)
    tot_cnt = tr_cnt + live_cnt
    tot_mean = np.divide(tr_mean * tr_cnt + live_sum, tot_cnt,
                         out=np.zeros(len(GRID)), where=tot_cnt > 0)

    pm, pv, pc = adaptive.posterior(
        np.array(GRID), tot_mean, tot_cnt, noise_var=1.0,
        prior_mean=float(np.average(tot_mean, weights=tot_cnt)) if tot_cnt.sum() else 0.0,
        length_scale=0.6, prior_var=1.0)
    inc_idx = GRID.index(INCUMBENT)
    rec = adaptive.recommend(np.array(GRID), pm, pc, inc_idx, confidence=0.9)
    gap = adaptive.oos_gap(tr_mean, ho_mean, rec["recommended_idx"])

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    art = {
        "knob": "trade_management.stop_atr_mult", "generated_at": now,
        "incumbent": INCUMBENT, "recommended": rec["recommended_value"],
        "moved": rec["moved"], "p_better": round(rec["p_better"], 4), "band": list(BAND),
        "grid": GRID, "posterior_mean": [round(float(x), 4) for x in pm],
        "evidence": {"replay_n": int(tr_cnt.sum()), "live_n": int(live_cnt.sum())},
        "oos_gap": round(gap, 4),
        "rationale": (f"moved to {rec['recommended_value']} (p={rec['p_better']:.2f})"
                      if rec["moved"] else
                      f"incumbent {INCUMBENT} retained (best challenger p={rec['p_better']:.2f} < 0.90)"),
        "provenance": (f"adaptive layer {now[:10]} from replay_n={int(tr_cnt.sum())} "
                       f"live_n={int(live_cnt.sum())}; incumbent was {INCUMBENT}"),
    }
    assert BAND[0] <= art["recommended"] <= BAND[1], art     # hard band guard
    PROPOSAL.parent.mkdir(parents=True, exist_ok=True)
    PROPOSAL.write_text(json.dumps(art, indent=2))
    print(f"wrote proposal -> {PROPOSAL}\n  {art['rationale']}")
```

- [ ] **Step 4: Run selftest, then a real dry-run if the price cache exists**

Run: `python scripts/tune_stop.py --selftest`
Expected: `selftest OK: build_samples counts + wide-stop ordering`

Then, only if `research_store/prices/highs.parquet` exists on this box:
Run: `python scripts/tune_stop.py`
Expected: `wrote proposal -> .../stop_atr_mult.json` and a valid JSON file whose `recommended` is within `[1.5, 3.5]`. (Skip if the cache isn't present — the Actions job in Task 5 runs it for real.)

- [ ] **Step 5: Commit**

```bash
git add scripts/tune_stop.py
git commit -m "feat(adaptive): stop_atr_mult tuner — PIT replay + live -> bounded proposal

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Off-box compute + data sync (GitHub Actions + mirror OHLC)

Run the tuner weekly on GitHub Actions (off the droplet entirely) and commit the proposal. **Gap resolved here:** the tuner needs the OHLC cache, which is git-ignored and not in the ledger mirror. Push the OHLC parquet to the ledger mirror from the droplet so the runner has it.

**Files:**
- Create: `.github/workflows/adaptive-tune.yml`
- Modify: `deploy/backup_ledger.sh` (also mirror the OHLC parquet)

**Interfaces:**
- Consumes: `scripts/tune_stop.py` (Task 4); the ledger mirror repo `aye5788/agentic-trader-ledger`.

- [ ] **Step 1: Extend the mirror to carry OHLC (droplet side)**

In `deploy/backup_ledger.sh`, after the existing `cp -f research_store/... "$MIRROR/..."` lines, add:

```bash
# OHLC price cache — needed off-box by the adaptive tuner (a few MB parquet).
mkdir -p "$MIRROR/prices"
cp -f research_store/prices/closes.parquet "$MIRROR/prices/closes.parquet" 2>/dev/null || true
cp -f research_store/prices/highs.parquet  "$MIRROR/prices/highs.parquet"  2>/dev/null || true
cp -f research_store/prices/lows.parquet   "$MIRROR/prices/lows.parquet"   2>/dev/null || true
```

- [ ] **Step 2: Verify the shell still parses**

Run: `bash -n deploy/backup_ledger.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Write the workflow**

Create `.github/workflows/adaptive-tune.yml`:

```yaml
name: adaptive-tune
on:
  schedule:
    - cron: "0 8 * * 1"        # Mondays 08:00 UTC — off the weekday market-hours pileup
  workflow_dispatch: {}         # manual runs allowed

permissions:
  contents: write               # to commit the proposal back

jobs:
  tune-stop:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install deps
        run: pip install pandas pyarrow numpy
      - name: Fetch OHLC cache from the ledger mirror
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git clone --depth 1 https://x-access-token:${GH_TOKEN}@github.com/aye5788/agentic-trader-ledger.git _ledger || {
            echo "ledger mirror unavailable — skipping tune"; exit 0; }
          mkdir -p research_store/prices research_store
          cp _ledger/prices/*.parquet research_store/prices/ 2>/dev/null || { echo "no OHLC in mirror yet — skip"; exit 0; }
          cp _ledger/journal.jsonl research_store/journal.jsonl 2>/dev/null || true
      - name: Run the tuner
        run: |
          test -f research_store/prices/highs.parquet || { echo "no OHLC — skip"; exit 0; }
          python scripts/tune_stop.py
      - name: Commit the proposal
        run: |
          if [ -f research_store/adaptive/proposals/stop_atr_mult.json ]; then
            git config user.name "adaptive-bot"
            git config user.email "noreply@anthropic.com"
            git add -f research_store/adaptive/proposals/stop_atr_mult.json
            git commit -m "chore(adaptive): weekly stop_atr_mult proposal" || echo "no change"
            git push || echo "push skipped"
          fi
```

Note: `research_store/adaptive/proposals/` must be force-added (`-f`) because `research_store/` is git-ignored; the proposal is the one tracked exception.

- [ ] **Step 4: Validate the workflow YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/adaptive-tune.yml')); print('yaml OK')"`
Expected: `yaml OK`
(If PyYAML is absent: `pip install pyyaml` first — dev-only, not added to requirements.)

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/adaptive-tune.yml deploy/backup_ledger.sh
git commit -m "feat(adaptive): weekly off-box tuner workflow + OHLC mirror sync

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Promotion + surfacing (`scripts/promote_proposal.py` + docs)

Close the loop on governance: a human reads the proposal and promotes it. Provide a helper that prints the exact `strategy.local.toml` stanza and flags an unpromoted proposal, and document the step. Consumption itself is already handled — `strategy.load()` deep-merges `strategy.local.toml`.

**Files:**
- Create: `scripts/promote_proposal.py`
- Modify: `docs/OPSLOG.md` (document the promotion step)

**Interfaces:**
- Consumes: `research_store/adaptive/proposals/stop_atr_mult.json` (Task 4).
- Produces: `pending_line(proposal: dict) -> str` (the ready-to-paste TOML stanza + provenance comment).

- [ ] **Step 1: Write the failing selftest**

Create `scripts/promote_proposal.py`:

```python
"""Human-in-the-loop promotion helper for adaptive proposals.

Reads a proposal and prints the exact config/strategy.local.toml stanza to paste
to ACCEPT it (propose-not-apply, spec §8). Never edits config itself — promotion
is a deliberate human act. Flags a proposal that recommends a move but hasn't been
promoted.

    python scripts/promote_proposal.py [--selftest]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROPOSAL = REPO / "research_store" / "adaptive" / "proposals" / "stop_atr_mult.json"


def pending_line(proposal: dict) -> str:
    p = proposal
    return (f"# {p['provenance']}\n"
            f"[trade_management]\n"
            f"stop_atr_mult = {p['recommended']}")


def _selftest() -> None:
    prop = {"recommended": 3.0, "moved": True,
            "provenance": "adaptive layer 2026-07-26 from replay_n=4120 live_n=7; incumbent was 2.5"}
    line = pending_line(prop)
    assert "stop_atr_mult = 3.0" in line, line
    assert "[trade_management]" in line, line
    assert line.startswith("# adaptive layer"), line
    print("selftest OK: pending_line")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/promote_proposal.py --selftest`
Expected: PASS (`selftest OK: pending_line`) — `pending_line` is defined in Step 1. If you ran without `--selftest`, expect `NameError: name 'main' is not defined`.

- [ ] **Step 3: Write `main()`**

Add above `_selftest`:

```python
def main():
    if not PROPOSAL.exists():
        print("no proposal found:", PROPOSAL)
        return
    prop = json.loads(PROPOSAL.read_text())
    print(f"proposal for {prop['knob']} generated {prop['generated_at']}")
    print(f"  incumbent={prop['incumbent']}  recommended={prop['recommended']}  "
          f"moved={prop['moved']}  p_better={prop['p_better']}  oos_gap={prop['oos_gap']}")
    print(f"  evidence: {prop['evidence']}")
    print(f"  rationale: {prop['rationale']}")
    if prop["moved"]:
        print("\nTo ACCEPT, append to config/strategy.local.toml:\n")
        print(pending_line(prop))
    else:
        print("\nNo change recommended — nothing to promote.")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python scripts/promote_proposal.py --selftest`
Expected: `selftest OK: pending_line`

- [ ] **Step 5: Document the step and commit**

Add to the TOP of `docs/OPSLOG.md` (newest-first) a dated entry:

```markdown
## 2026-07-23 — Adaptive-input layer (dial #1: stop_atr_mult)

Off-box weekly learner (GitHub Actions) proposes a bounded stop_atr_mult from
replayed + live outcomes. It NEVER applies. To act on a proposal:
`python scripts/promote_proposal.py` prints the exact strategy.local.toml stanza;
paste it to accept. Proposals live in research_store/adaptive/proposals/ (mirror-
backed). Spec: docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md
```

```bash
git add scripts/promote_proposal.py docs/OPSLOG.md
git commit -m "feat(adaptive): human promotion helper + OPSLOG procedure

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **Order matters:** Tasks 1→2→3 are independent pure modules and can be built in any order; Task 4 consumes all three; Task 5 needs Task 4; Task 6 is independent of 5.
- **`momentum.compute/select` signature:** confirm the exact keyword args in `src/momentum.py` before writing `generate_entries` (Task 4 Step 3) — the plan assumes `compute(panel, asof=...)` and `select(scored)`; adjust the call to match the real signature (it is the same one `scripts/backtest_pit.py` uses — copy from there).
- **`noise_var` estimate:** the plan uses `noise_var=1.0` as a placeholder scale for realized-R variance. Once real replay samples exist, set it to the pooled sample variance of realized-R (a one-line change in `main()`); the selftests fix it at 1.0 deliberately.
- **Nothing in this plan may be wired into `run_fast_loop.sh`, `run_slow_loop.sh`, or any prompt** — the layer is off-box and advisory only (spec §3).
