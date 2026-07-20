# Short-Term Momentum Early-Warning — Phase 1 (build + replay) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure short-term-momentum indicator module (MACD + RSI + relative-strength-vs-SPY, established indicators only) and a historical stop-out replay that decides whether it gives the risk review a useful *early* warning — before wiring anything into the live review.

**Architecture:** A new pure module `src/ti_signals.py` (no I/O — daily close Series in, per-name established-TI reads + a "weakening" tag out), consumed by an offline `scripts/ti_replay.py` that reconstructs the run-up to real stop-out events and measures how many days ahead the tag fires (and its false-fire base rate). NO change to `scripts/risk_review.py` in this phase — that is Phase 2, gated on this replay.

**Tech Stack:** Python 3.12 (project `.venv`), pandas/numpy, the cached `research_store/prices/closes.parquet` (daily closes: 150 names + ETFs + SPY, multi-year). Same pure-function + `_selftest()` discipline as `src/momentum.py` and `src/concentration.py`.

## Global Constraints

- `src/ti_signals.py` is PURE — its only inputs are the price Series passed in; no file/network I/O.
- **Established indicators only** — MACD(12,26,9), Wilder RSI(14), relative-strength line = close/SPY. No bespoke composite (construct-validity + stateless-agent context cost, per the spec).
- **Two dimensions, confirmed in conjunction:** absolute leg (MACD hist / RSI-50) AND relative leg (RS-vs-SPY). The `weakening` tag requires BOTH soft.
- **No live-path change.** Do not touch `scripts/risk_review.py`, `prompts/risk_review.md`, or any loop in this phase.
- Benchmark for the relative leg = **SPY** (per-sector deferred — no name→sector map exists).
- Selftests run under the project venv: `python` = `/opt/agentic-trader/.venv/bin/python`.
- Repo convention: pure modules expose `_selftest()` run via `__main__`, registered in `deploy/run_selftests.sh` (matches `momentum.py`, `concentration.py`).

---

### Task 1: The pure indicator module

**Files:**
- Create: `src/ti_signals.py`
- Modify: `deploy/run_selftests.sh`

**Interfaces:**
- Produces:
  - `ti_signals.macd(closes: pd.Series, fast=12, slow=26, signal=9) -> tuple[pd.Series, pd.Series, pd.Series]` — (macd_line, signal_line, hist).
  - `ti_signals.rsi(closes: pd.Series, period=14) -> pd.Series` — Wilder RSI (0..100).
  - `ti_signals.compute(closes: pd.Series, spy: pd.Series, asof, params: dict | None = None) -> dict` — per-name read as of `asof`: keys `macd_hist, macd_hist_prev, macd_soft, rsi, rsi_soft, rs, rs_ma, rel_soft, tag_macd, tag_rsi` (tags ∈ {"ok","watch","weakening","n/a"}).
  - `ti_signals._selftest() -> None`.

- [ ] **Step 1: Write the module with its selftest**

Create `src/ti_signals.py`:
```python
"""Short-term momentum early-warning indicators for the risk review (Piece 3).
PURE: daily close Series in, per-name established-TI reads out — no I/O.
Two dimensions, established indicators, confirmed in conjunction (no TI in isolation):
  absolute leg (name's own momentum decelerating): MACD(12,26,9) histogram, RSI(14) vs 50
  relative leg (losing edge vs the market): relative-strength line = close/SPY vs its MA
The stateless risk review is fed the VALUES + a light 'weakening' tag; it judges. Only
established indicators, so the model reads them cold (no bespoke-score manual to re-feed).
"""
import pandas as pd


def macd(closes, fast=12, slow=26, signal=9):
    """Standard MACD. Returns (macd_line, signal_line, hist) as Series."""
    ema_f = closes.ewm(span=fast, adjust=False).mean()
    ema_s = closes.ewm(span=slow, adjust=False).mean()
    line = ema_f - ema_s
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def rsi(closes, period=14):
    """Wilder's RSI as a Series (0..100)."""
    d = closes.diff()
    gain = d.clip(lower=0.0)
    loss = (-d).clip(lower=0.0)
    ag = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    al = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = ag / al.replace(0.0, pd.NA)
    return 100.0 - 100.0 / (1.0 + rs)


def compute(closes, spy, asof, params=None):
    """Per-name early-warning read as of `asof`. closes/spy: daily close Series
    (index=dates) with >= slow+signal rows through asof. Returns values + soft flags +
    two 'weakening' tags (MACD-based and RSI-based pairings; both require the relative
    leg ALSO soft — no indicator acts alone)."""
    p = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
         "rsi_period": 14, "rsi_line": 50.0, "rs_ma": 20, **(params or {})}
    c = closes.loc[:asof].dropna()
    s = spy.loc[:asof].reindex(c.index).ffill()
    if len(c) < p["macd_slow"] + p["macd_signal"] + 1:
        return {"tag_macd": "n/a", "tag_rsi": "n/a", "reason": "insufficient history"}

    line, sig, hist = macd(c, p["macd_fast"], p["macd_slow"], p["macd_signal"])
    h0, h1 = float(hist.iloc[-1]), float(hist.iloc[-2])
    macd_soft = h0 < 0.0 and h0 <= h1                       # histogram negative AND not improving

    r = float(rsi(c, p["rsi_period"]).iloc[-1])
    rsi_soft = r < p["rsi_line"]                            # below the 50 regime line

    rs_line = c / s
    rs_ma = rs_line.rolling(p["rs_ma"]).mean()
    rs0, rsm = float(rs_line.iloc[-1]), float(rs_ma.iloc[-1])
    rel_soft = rs0 < rsm                                    # relative-strength below its MA

    def tag(abs_soft):
        if abs_soft and rel_soft:
            return "weakening"
        return "watch" if (abs_soft or rel_soft) else "ok"

    return {"macd_hist": round(h0, 4), "macd_hist_prev": round(h1, 4), "macd_soft": macd_soft,
            "rsi": round(r, 1), "rsi_soft": rsi_soft,
            "rs": round(rs0, 5), "rs_ma": round(rsm, 5), "rel_soft": rel_soft,
            "tag_macd": tag(macd_soft), "tag_rsi": tag(rsi_soft)}


def _selftest():
    import numpy as np

    idx = pd.date_range("2025-01-01", periods=80, freq="B")
    # a clean uptrend that ROLLS OVER over the last ~15 days, while SPY keeps rising:
    # the name is both decelerating (absolute) AND lagging the market (relative).
    px = pd.Series(np.r_[np.linspace(100, 145, 65), np.linspace(145, 118, 15)], index=idx)
    spy = pd.Series(np.linspace(400, 480, 80), index=idx)
    asof = idx[-1]
    out = compute(px, spy, asof)
    assert out["macd_soft"] and out["rel_soft"], out
    assert out["tag_macd"] == "weakening", out
    assert out["rsi"] < 50, out
    print("ti_signals selftest OK: rollover vs rising market -> weakening")

    # a healthy leader in a flat market: nothing soft -> ok
    strong = pd.Series(np.linspace(100, 185, 80), index=idx)
    flat = pd.Series(np.full(80, 400.0), index=idx)
    o2 = compute(strong, flat, asof)
    assert not o2["macd_soft"] and not o2["rel_soft"], o2
    assert o2["tag_macd"] == "ok" and o2["rsi"] >= 50, o2
    print("ti_signals selftest OK: strong leader -> ok")

    # insufficient history -> n/a (no crash)
    short = pd.Series(np.linspace(100, 110, 10), index=idx[:10])
    assert compute(short, spy, idx[9])["tag_macd"] == "n/a"
    print("ti_signals selftest OK: insufficient history -> n/a")


if __name__ == "__main__":
    _selftest()
```

- [ ] **Step 2: Run the selftest, verify it PASSES**

Run: `cd /opt/agentic-trader && .venv/bin/python src/ti_signals.py`
Expected: three lines —
```
ti_signals selftest OK: rollover vs rising market -> weakening
ti_signals selftest OK: strong leader -> ok
ti_signals selftest OK: insufficient history -> n/a
```
(Authored to pass. If an assert fails, the synthetic series or a threshold is off — the standard fix is to deepen the rollover in `px` until MACD hist clearly turns negative; do NOT weaken the assertions.)

- [ ] **Step 3: Register the selftest**

In `deploy/run_selftests.sh`, add `"src/ti_signals.py"` to the `SELFTESTS` array (after `"src/concentration.py"`).

Run: `cd /opt/agentic-trader && deploy/run_selftests.sh 2>&1 | tail -6`
Expected: all selftests PASS, including the three ti_signals lines.

- [ ] **Step 4: Commit**

```bash
cd /opt/agentic-trader
git add src/ti_signals.py deploy/run_selftests.sh
git commit -m "feat(ti-signals): pure MACD+RSI+RS-vs-SPY early-warning module (+selftest)"
```

---

### Task 2: The stop-out replay (the go/no-go deliverable)

**Files:**
- Create: `config/stopout_events.csv`
- Create: `scripts/ti_replay.py`

**Interfaces:**
- Consumes: `ti_signals.compute` (Task 1); `research_store/prices/closes.parquet`.
- Produces: a printed table (event × pairing × days-of-lead) + per-pairing hit-rate and false-fire base rate + a recommendation. No return value; it's a report.

**Goal of this task:** for each real stop-out, walk the trading days *before* the stop and find how many days ahead the `weakening` tag first fired — for the MACD pairing and the RSI pairing — plus how often the tag fires across the whole universe on those dates (the false-fire base rate). This table is the decision input.

- [ ] **Step 1: Seed the event set**

Create `config/stopout_events.csv` (the documented 2026-07-17 storage/semi cluster — the event this piece exists to catch; extend later from the journal/cooldown):
```csv
ticker,stop_date
ALAB,2026-07-17
AMD,2026-07-17
BE,2026-07-17
INTC,2026-07-17
LRCX,2026-07-17
SNDK,2026-07-17
STX,2026-07-17
WDC,2026-07-17
XLK,2026-07-17
EEM,2026-07-17
```
(Stop dates are the documented cluster date; refine per-name from `research_store` journal if a name's precise stop differs. A name absent from `closes.parquet` is skipped with a note.)

- [ ] **Step 2: Write the replay script**

Create `scripts/ti_replay.py`:
```python
"""Piece-3 Phase-1 replay: did the short-term-momentum 'weakening' tag fire BEFORE
real stop-outs (giving the risk review an earlier exit), and does it discriminate
(low false-fire base rate)? Offline, no live path. Reads config/stopout_events.csv +
the cached daily closes; prints the go/no-go table.

    .venv/bin/python scripts/ti_replay.py [--window 5]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import ti_signals as ti           # noqa: E402

CLOSES = REPO / "research_store" / "prices" / "closes.parquet"
EVENTS = REPO / "config" / "stopout_events.csv"


def first_fire(closes, spy, ticker, stop_date, window, key):
    """Earliest of the `window` trading days before stop_date on which compute()[key]
    == 'weakening'. Returns lead in trading days (>=1) or None."""
    if ticker not in closes.columns:
        return "no-data"
    idx = closes.index
    if stop_date not in idx:                      # snap to the last trading day <= stop
        prior = idx[idx <= stop_date]
        if len(prior) == 0:
            return None
        stop_date = prior[-1]
    si = idx.get_loc(stop_date)
    for lead in range(window, 0, -1):             # oldest day in the window first
        d = idx[si - lead]
        tag = ti.compute(closes[ticker], spy, d).get(key)
        if tag == "weakening":
            return lead
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=5, help="trading days before stop to scan")
    args = ap.parse_args()

    if not CLOSES.exists():
        sys.exit("no price cache — run scripts/fetch_prices.py first")
    closes = pd.read_parquet(CLOSES).sort_index()
    spy = closes["SPY"]
    events = pd.read_csv(EVENTS)
    events["stop_date"] = pd.to_datetime(events["stop_date"])

    print(f"replay: {len(events)} events, window {args.window}d before stop\n")
    print(f"{'ticker':8}{'stop':12}{'MACD lead':>11}{'RSI lead':>10}")
    print("-" * 41)
    hits = {"tag_macd": [], "tag_rsi": []}
    for _, e in events.iterrows():
        lm = first_fire(closes, spy, e.ticker, e.stop_date, args.window, "tag_macd")
        lr = first_fire(closes, spy, e.ticker, e.stop_date, args.window, "tag_rsi")
        for key, v in (("tag_macd", lm), ("tag_rsi", lr)):
            if isinstance(v, int):
                hits[key].append(v)
        fmt = lambda v: (f"{v}d ahead" if isinstance(v, int) else str(v or "—"))
        print(f"{e.ticker:8}{str(e.stop_date.date()):12}{fmt(lm):>11}{fmt(lr):>10}")

    # false-fire base rate: fraction of ALL universe names flagged 'weakening' on the
    # event dates (if the tag fires on everything, a 'hit' means nothing).
    uni = [c for c in closes.columns if c != "SPY"]
    dates = sorted(events["stop_date"].map(
        lambda d: closes.index[closes.index <= d][-1]).unique())
    base = {"tag_macd": 0, "tag_rsi": 0, "n": 0}
    for d in dates:
        for t in uni:
            o = ti.compute(closes[t], spy, d)
            if o.get("tag_macd") == "n/a":
                continue
            base["n"] += 1
            base["tag_macd"] += o["tag_macd"] == "weakening"
            base["tag_rsi"] += o["tag_rsi"] == "weakening"

    print("\n--- summary ---")
    n_ev = len(events)
    for key, label in (("tag_macd", "MACD+RS"), ("tag_rsi", "RSI50+RS")):
        h = hits[key]
        hit_rate = len(h) / n_ev if n_ev else 0
        avg_lead = sum(h) / len(h) if h else 0
        ff = base[key] / base["n"] if base["n"] else 0
        print(f"{label:10} hit {len(h)}/{n_ev} ({hit_rate:.0%})  avg lead {avg_lead:.1f}d"
              f"  |  false-fire base rate {ff:.0%}")
    print("\nPASS if a pairing leads the stop by >=1-2d on a majority of events with a")
    print("false-fire base rate well below its hit rate. Else drop it (stops already work).")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the replay, produce the table**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/ti_replay.py 2>&1 | tail -30`
Expected: a per-event table (MACD lead / RSI lead in days) + a summary with hit-rate, average lead, and false-fire base rate for each pairing. Confirm it runs cleanly and every event resolves to a lead, `—` (no fire), or `no-data`.

- [ ] **Step 4: Commit**

```bash
cd /opt/agentic-trader
git add config/stopout_events.csv scripts/ti_replay.py
git commit -m "feat(ti-replay): stop-out early-warning replay + 07-17 event seed (Phase-1 gate)"
```

- [ ] **Step 5: Produce the deliverable (the decision input)**

Run the replay once more and save the table:
`cd /opt/agentic-trader && .venv/bin/python scripts/ti_replay.py | tee /tmp/ti_replay.txt`
Summarize for Aaron: **does either pairing (MACD+RS or RSI50+RS) fire `weakening` ≥1–2 trading days before the stop on a majority of the 07-17 cluster, with a false-fire base rate clearly below its hit rate?**
- **PASS** → recommend a Phase-2 spec wiring the winning pairing into `risk_review.py` facts + `prompts/risk_review.md`.
- **FAIL** (fires *at* the stop, or fires on everything) → recommend dropping it; the intraday stops already handle this. A "no" is a valid outcome (the Piece-2 discipline).

---

## Notes for the implementer

- **This phase touches NO live path.** `src/ti_signals.py`, `scripts/ti_replay.py`, `config/stopout_events.csv`, and the `deploy/run_selftests.sh` one-liner only. Never `risk_review.py` / `prompts/`.
- Everything runs under `.venv/bin/python`; no moomoo, no network.
- The `weakening` tag is deliberately conjunction-gated (both legs soft) — a single soft leg is only `watch`. Do not relax that to chase hits; a signal that fires on one leg alone is the isolation failure the design rejects.
- If the replay shows daily bars are too slow (fires only at the stop), that's a real finding — report it; finer intraday bars would be a follow-on, not a silent patch.
