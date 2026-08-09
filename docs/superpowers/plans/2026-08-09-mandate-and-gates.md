# Mandate and Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the falsifiable mandate as a continuously-measured, three-state module, and strip *selection* out of governance so gates reject on safety only.

**Architecture:** A new pure module `src/mandate.py` evaluates four criteria against data that already exists on disk (`research_store/history/equity.jsonl`, `src/marks.py`, the journal's `outcome` events, `research_store/prices/closes.parquet`). `src/governance.py` gains an account guard and a liquidity gate, and loses the universe whitelist as a *selection* restriction. Nothing about the agent changes in this plan — this is the terms and the guardrails, which everything downstream depends on.

**Tech Stack:** Python 3.12 (`.venv`), `tomllib`, `pandas` (SPY closes only), stdlib elsewhere. No new dependencies.

## Global Constraints

- **Plan 1 of 5** for `docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md`. Read the spec first.
- **Live money.** Judge every risk on mechanism and consequence, never on the size of the book. Never print or commit secrets.
- **This plan changes no live trading behaviour.** Nothing here places, cancels, or blocks a real order. `src/mandate.py` is read-only; the governance changes are additive except Task 10.
- **Testing convention is module-local `--selftest`, NOT pytest.** This repo has no pytest, no `tests/` tree. Every module exposes `_selftest()` with plain `assert`s and a `if __name__ == "__main__": if "--selftest" in sys.argv` block. Follow it.
- **Two runtimes.** Everything in this plan runs under `.venv/bin/python` (3.12). Nothing here imports `moomoo`, so nothing here needs system python3.
- **Every criterion is three-state:** `PASS` / `FAIL` / `INSUFFICIENT_DATA`. `INSUFFICIENT_DATA` must never render as a pass.
- **Blocking vs informational.** Criteria 1–2 are blocking (safety). Criteria 3–4 are informational (performance) and must never gate an order.
- Commit after every task. Branch: work on `agent/repository-inspection` unless told otherwise.

**Verified data schemas (2026-08-09) — do not guess these:**

```
research_store/history/equity.jsonl   {"date":"YYYY-MM-DD","ts":iso,"value":float,"invested":float,"cash":float}
                                      22 rows as of 2026-08-07

marks.load()                          {"account_number":str,"as_of":str,"ts":str,"marked_at":str,
                                       "cash":float,"invested":float,"account_value":float,
                                       "positions":{SYM:{"qty":float,"avg_cost":float,
                                                         "mark":float,"value":float,"pnl":float}}}

journal `outcome` event               {"at":iso,"decision_id":"SYM:YYYY-MM-DD","event":"outcome",
                                       "symbol":str,
                                       "outcome":{"entry_price":float,"exit_price":float,
                                                  "exit_reason":str,"hit_stop":bool,"hit_target":bool,
                                                  "holding_days":int,"pnl_pct":float,
                                                  "return_vs_spy":float|None,"status":str}}
                                      16 rows. NOTE: pnl_pct only — NO dollar P&L.
```

---

### Task 1: Mandate config and loader

**Files:**
- Create: `config/mandate.toml`
- Create: `src/mandate.py`

**Interfaces:**
- Produces: `mandate.load() -> dict`; constants `mandate.PASS`, `mandate.FAIL`, `mandate.INSUFFICIENT`.

- [ ] **Step 1: Write `config/mandate.toml`**

```toml
# THE MANDATE — the falsifiable terms the agent operates under.
#
# This file states WHAT SUCCESS IS. It states nothing about HOW to trade: no
# universe, no entry signal, no exit rule, no sizing formula. Those belong to the
# agent (docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md §3).
#
# If a line here reads as an instruction about WHAT to trade, that is a defect in
# this file. Report it rather than obeying it.
#
# Every criterion is three-state: PASS / FAIL / INSUFFICIENT_DATA.
# INSUFFICIENT_DATA is never a pass.

# --- BLOCKING (safety). A FAIL here makes the machine act. -------------------
[drawdown]
max_pct = 0.15        # close-to-close, from the all-time high-water mark.
                      #   Breach -> mechanical flatten. NEVER measured intraday:
                      #   an intraday measure fires the flatten on noise.

[concentration]
max_position_pct = 0.15   # no single position above this share of equity, at any mark

# --- INFORMATIONAL (performance). These judge whether autonomy is working.
# --- They NEVER gate an order.  ----------------------------------------------
[pnl_concentration]
window_days        = 90
max_single_share   = 0.40   # no one closed round-trip above this share of realized P&L
min_distinct_names = 4      # ...and at least this many distinct names closed

[relative_return]
window_days = 60            # trading days
benchmark   = "SPY"
```

- [ ] **Step 2: Write the failing test in `src/mandate.py`**

```python
"""THE MANDATE — continuous, falsifiable evaluation of the terms the agent works
under (spec: docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md §5).

Pure functions over data that already exists on disk. No network, no broker, no
clock of its own — callers pass `asof`. Three-state by design: a criterion that
cannot be computed reports INSUFFICIENT_DATA and MUST NOT read as a pass.

Run the tests:  .venv/bin/python src/mandate.py --selftest
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT_DATA"


def load(path: str = "config/mandate.toml") -> dict:
    """Load the mandate terms. Raises if absent — an unstated mandate is not a
    permissive one, and must never silently default."""
    p = REPO / path
    if not p.exists():
        raise FileNotFoundError(f"mandate not found at {p}; the terms must be explicit")
    with p.open("rb") as fh:
        return tomllib.load(fh)


def _selftest() -> None:
    m = load()
    assert m["drawdown"]["max_pct"] == 0.15, m["drawdown"]
    assert m["concentration"]["max_position_pct"] == 0.15, m["concentration"]
    assert m["pnl_concentration"]["window_days"] == 90
    assert m["pnl_concentration"]["max_single_share"] == 0.40
    assert m["pnl_concentration"]["min_distinct_names"] == 4
    assert m["relative_return"]["window_days"] == 60
    assert m["relative_return"]["benchmark"] == "SPY"
    assert PASS != FAIL != INSUFFICIENT
    print("selftest OK: mandate loads, terms match")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 3: Run it to verify it passes**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: `selftest OK: mandate loads, terms match`

- [ ] **Step 4: Commit**

```bash
git add config/mandate.toml src/mandate.py
git commit -m "feat(mandate): the terms, as an explicit falsifiable config

Separates WHAT SUCCESS IS from HOW to trade. Loader raises rather than
defaulting: an unstated mandate must never silently become a permissive one."
```

---

### Task 2: Criterion 1 — drawdown (blocking)

**Files:**
- Modify: `src/mandate.py`

**Interfaces:**
- Consumes: `load()`, `PASS`/`FAIL`/`INSUFFICIENT` from Task 1.
- Produces: `drawdown(equity: list[float], max_pct: float) -> dict` returning
  `{"criterion":"drawdown","state":str,"value":float|None,"limit":float,"room":float|None,"reason":str}`.

- [ ] **Step 1: Write the failing test — append inside `_selftest()` before the print**

```python
    # --- criterion 1: drawdown ------------------------------------------------
    md = m["drawdown"]["max_pct"]
    # peak 100 -> 95 is a 5% drawdown against a 15% limit: PASS, 10% of room left
    r = drawdown([80.0, 100.0, 95.0], md)
    assert r["state"] == PASS, r
    assert abs(r["value"] + 0.05) < 1e-9, r
    assert abs(r["room"] - 0.10) < 1e-9, r
    # peak 100 -> 84 breaches 15%
    assert drawdown([100.0, 84.0], md)["state"] == FAIL
    # exactly at the limit is NOT a breach (breach is strictly worse than the limit)
    assert drawdown([100.0, 85.0], md)["state"] == PASS
    # the peak is all-time and does not follow the book down
    assert abs(drawdown([100.0, 90.0, 92.0], md)["value"] + 0.08) < 1e-9
    # fewer than two points cannot express a drawdown
    assert drawdown([100.0], md)["state"] == INSUFFICIENT
    assert drawdown([], md)["state"] == INSUFFICIENT
    # a non-positive peak is undefined, not a pass
    assert drawdown([0.0, 0.0], md)["state"] == INSUFFICIENT
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: FAIL with `NameError: name 'drawdown' is not defined`

- [ ] **Step 3: Implement — add above `_selftest()`**

```python
def drawdown(equity: list[float], max_pct: float) -> dict:
    """Criterion 1 (BLOCKING). Close-to-close drawdown from the all-time high-water
    mark. `equity` is the ordered daily close series, oldest first.

    Never measured intraday: an intraday measure fires the flatten on noise.
    """
    out = {"criterion": "drawdown", "state": INSUFFICIENT, "value": None,
           "limit": max_pct, "room": None, "reason": ""}
    vals = [float(v) for v in equity if v is not None]
    if len(vals) < 2:
        out["reason"] = f"need 2+ daily closes, have {len(vals)}"
        return out
    peak = max(vals)
    if peak <= 0:
        out["reason"] = "non-positive peak equity; drawdown undefined"
        return out
    dd = vals[-1] / peak - 1.0
    out["value"] = dd
    out["room"] = abs(max_pct) + dd          # how much further it may fall
    out["state"] = FAIL if dd < -abs(max_pct) else PASS
    out["reason"] = (f"{dd:.2%} from peak {peak:.2f} "
                     f"(limit {abs(max_pct):.0%})")
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: `selftest OK: mandate loads, terms match`

- [ ] **Step 5: Commit**

```bash
git add src/mandate.py
git commit -m "feat(mandate): criterion 1 -- drawdown from the all-time high-water mark

Close-to-close only. An intraday measure would fire the mandate flatten on
noise. The peak never follows the book down, so the criterion cannot be reset
by a bad stretch."
```

---

### Task 3: Criterion 2 — concentration (blocking)

**Files:**
- Modify: `src/mandate.py`

**Interfaces:**
- Produces: `concentration(positions: dict, account_value: float, max_pct: float) -> dict`.
  `positions` is `marks.load()["positions"]` — `{SYM: {"value": float, ...}}`.

- [ ] **Step 1: Write the failing test — append inside `_selftest()`**

```python
    # --- criterion 2: concentration -------------------------------------------
    mc = m["concentration"]["max_position_pct"]
    pos = {"AAA": {"value": 10.0}, "BBB": {"value": 5.0}}
    r = concentration(pos, 100.0, mc)          # worst is 10% against a 15% limit
    assert r["state"] == PASS, r
    assert r["worst_symbol"] == "AAA" and abs(r["value"] - 0.10) < 1e-9, r
    assert abs(r["room"] - 0.05) < 1e-9, r
    # 20% of equity in one name breaches
    r = concentration({"AAA": {"value": 20.0}}, 100.0, mc)
    assert r["state"] == FAIL and r["worst_symbol"] == "AAA", r
    # exactly at the limit is not a breach
    assert concentration({"AAA": {"value": 15.0}}, 100.0, mc)["state"] == PASS
    # a flat book trivially passes
    assert concentration({}, 100.0, mc)["state"] == PASS
    # unusable account value is undefined, not a pass
    assert concentration(pos, 0.0, mc)["state"] == INSUFFICIENT
    # a position with no mark cannot be assessed -- must not be silently skipped
    assert concentration({"AAA": {"value": None}}, 100.0, mc)["state"] == INSUFFICIENT
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: FAIL with `NameError: name 'concentration' is not defined`

- [ ] **Step 3: Implement — add above `_selftest()`**

```python
def concentration(positions: dict, account_value: float, max_pct: float) -> dict:
    """Criterion 2 (BLOCKING). Largest single position as a share of equity.

    A position carrying no usable mark returns INSUFFICIENT rather than being
    skipped: an unmeasurable position is exactly the one most likely to be the
    concentrated one.
    """
    out = {"criterion": "concentration", "state": INSUFFICIENT, "value": None,
           "limit": max_pct, "room": None, "worst_symbol": None, "reason": ""}
    try:
        av = float(account_value)
    except (TypeError, ValueError):
        av = 0.0
    if av <= 0:
        out["reason"] = "account value unusable; concentration undefined"
        return out
    shares = {}
    for sym, p in (positions or {}).items():
        v = p.get("value")
        if v is None or not isinstance(v, (int, float)):
            out["reason"] = f"{sym} has no usable mark; concentration unmeasurable"
            return out
        shares[sym] = float(v) / av
    if not shares:
        out.update(state=PASS, value=0.0, room=abs(max_pct),
                   reason="no positions held")
        return out
    worst = max(shares, key=lambda s: shares[s])
    out["worst_symbol"] = worst
    out["value"] = shares[worst]
    out["room"] = abs(max_pct) - shares[worst]
    out["state"] = FAIL if shares[worst] > abs(max_pct) else PASS
    out["reason"] = (f"{worst} at {shares[worst]:.1%} of equity "
                     f"(limit {abs(max_pct):.0%})")
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: `selftest OK: mandate loads, terms match`

- [ ] **Step 5: Commit**

```bash
git add src/mandate.py
git commit -m "feat(mandate): criterion 2 -- position concentration

A position with no usable mark returns INSUFFICIENT rather than being skipped.
An unmeasurable position is exactly the one most likely to be the concentrated
one, so skipping it would turn the criterion into a silent pass."
```

---

### Task 4: Criterion 3 — P&L concentration (informational)

**Files:**
- Modify: `src/mandate.py`

**Interfaces:**
- Produces: `pnl_concentration(outcomes: list[dict], asof: str, window_days: int, max_share: float, min_names: int) -> dict`.
  `outcomes` are journal `outcome` events (see Global Constraints for the schema).

**Context the implementer needs:** outcome records carry `pnl_pct` but **no dollar P&L** — position size was never recorded. This criterion is a share of realized *dollars*, so it is only computable for round-trips whose record carries `realized_usd`. That field arrives with the executed-quantity work of 2026-08-09 (`604a75d`) and is therefore forward-only. Records lacking it must produce `INSUFFICIENT_DATA`, never a pass.

- [ ] **Step 1: Write the failing test — append inside `_selftest()`**

```python
    # --- criterion 3: P&L concentration (informational) -----------------------
    pc = m["pnl_concentration"]
    def _o(sym, day, usd):
        return {"event": "outcome", "symbol": sym, "at": f"2026-08-{day:02d}T00:00:00Z",
                "outcome": {"realized_usd": usd}}
    # four names, no single one above 40% of $100 realized -> PASS
    good = [_o("A", 1, 30.0), _o("B", 2, 30.0), _o("C", 3, 25.0), _o("D", 4, 15.0)]
    r = pnl_concentration(good, "2026-08-09", pc["window_days"],
                          pc["max_single_share"], pc["min_distinct_names"])
    assert r["state"] == PASS, r
    assert abs(r["value"] - 0.30) < 1e-9 and r["distinct_names"] == 4, r
    # one name carrying 70% of the result -> FAIL
    hot = [_o("A", 1, 70.0), _o("B", 2, 10.0), _o("C", 3, 10.0), _o("D", 4, 10.0)]
    assert pnl_concentration(hot, "2026-08-09", 90, 0.40, 4)["state"] == FAIL
    # too few distinct names -> FAIL even when no single share is too big
    thin = [_o("A", 1, 30.0), _o("B", 2, 30.0), _o("A", 3, 40.0)]
    assert pnl_concentration(thin, "2026-08-09", 90, 0.40, 4)["state"] == FAIL
    # a LOSS-making window has no share-of-profit to test -> INSUFFICIENT, not PASS
    down = [_o("A", 1, -30.0), _o("B", 2, 10.0)]
    assert pnl_concentration(down, "2026-08-09", 90, 0.40, 4)["state"] == INSUFFICIENT
    # records without realized_usd (every record before 2026-08-09) -> INSUFFICIENT
    legacy = [{"event": "outcome", "symbol": "A", "at": "2026-08-01T00:00:00Z",
               "outcome": {"pnl_pct": 0.011}}]
    r = pnl_concentration(legacy, "2026-08-09", 90, 0.40, 4)
    assert r["state"] == INSUFFICIENT and "realized_usd" in r["reason"], r
    # nothing closed at all -> INSUFFICIENT
    assert pnl_concentration([], "2026-08-09", 90, 0.40, 4)["state"] == INSUFFICIENT
    # outside the window is excluded
    old = [_o("A", 1, 100.0)]
    assert pnl_concentration(old, "2026-12-01", 90, 0.40, 4)["state"] == INSUFFICIENT
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: FAIL with `NameError: name 'pnl_concentration' is not defined`

- [ ] **Step 3: Implement — add above `_selftest()`. Also add `from datetime import date, datetime, timedelta` to the imports at the top of the file.**

```python
def _as_date(ts: str):
    """Parse an ISO timestamp to a date. Returns None if unparseable — callers
    must treat that as missing data, never as in-window."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    except Exception:
        return None


def pnl_concentration(outcomes: list[dict], asof: str, window_days: int,
                      max_share: float, min_names: int) -> dict:
    """Criterion 3 (INFORMATIONAL — never gates an order). Over the trailing
    window: no single closed round-trip above `max_share` of realized P&L, and at
    least `min_names` distinct names closed.

    Requires `realized_usd` on the outcome record. Records predating 2026-08-09
    carry only `pnl_pct` and cannot support a share-of-dollars test; they report
    INSUFFICIENT rather than passing by default.
    """
    out = {"criterion": "pnl_concentration", "state": INSUFFICIENT, "value": None,
           "limit": max_share, "room": None, "distinct_names": 0, "reason": ""}
    cutoff = _as_date(asof)
    if cutoff is None:
        out["reason"] = f"unparseable asof {asof!r}"
        return out
    start = cutoff - timedelta(days=window_days)

    rows, missing = [], 0
    for e in outcomes or []:
        if e.get("event") != "outcome":
            continue
        d = _as_date(e.get("at", ""))
        if d is None or not (start <= d <= cutoff):
            continue
        usd = (e.get("outcome") or {}).get("realized_usd")
        if usd is None or not isinstance(usd, (int, float)):
            missing += 1
            continue
        rows.append((e.get("symbol"), float(usd)))

    if not rows:
        out["reason"] = (f"no closed round-trip in the last {window_days}d carries "
                         f"realized_usd ({missing} lacked it)")
        return out
    total = sum(u for _, u in rows)
    if total <= 0:
        out["reason"] = (f"trailing realized P&L is {total:.2f}; a share-of-profit "
                         "test has no meaning without profit")
        return out

    biggest = max(u for _, u in rows)
    share = biggest / total
    names = len({s for s, _ in rows})
    out.update(value=share, distinct_names=names, room=abs(max_share) - share)
    ok = share <= abs(max_share) and names >= min_names
    out["state"] = PASS if ok else FAIL
    out["reason"] = (f"largest round-trip {share:.0%} of {total:.2f} realized "
                     f"(limit {abs(max_share):.0%}), {names} distinct names "
                     f"(min {min_names})")
    if missing:
        out["reason"] += f"; {missing} record(s) lacked realized_usd and were excluded"
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: `selftest OK: mandate loads, terms match`

- [ ] **Step 5: Commit**

```bash
git add src/mandate.py
git commit -m "feat(mandate): criterion 3 -- P&L concentration, informational

Requires realized_usd, which outcome records only carry from 2026-08-09 (604a75d)
-- before that they held pnl_pct and no position size. Legacy records report
INSUFFICIENT rather than passing. A loss-making window also reports INSUFFICIENT:
'no single trade exceeded 40% of nothing' must never read as a pass."
```

---

### Task 5: Criterion 4 — relative return (informational)

**Files:**
- Modify: `src/mandate.py`

**Interfaces:**
- Produces: `relative_return(equity: list[float], benchmark: list[float], window_days: int) -> dict`.
  Both series ordered oldest-first, already aligned to the same trading days by the caller.

**Context the implementer needs:** `equity.jsonl` held **22 rows** on 2026-08-09 against the 60 this needs, so this criterion reports `INSUFFICIENT_DATA` until roughly 2026-10. That is expected and correct — it must not be worked around. Assumes no deposits or withdrawals over the window; the equity series is a pure mark-to-market curve.

- [ ] **Step 1: Write the failing test — append inside `_selftest()`**

```python
    # --- criterion 4: relative return (informational) -------------------------
    # book +10%, SPY +5% over the window -> PASS
    eq = [100.0] * 58 + [100.0, 110.0]
    spy = [50.0] * 58 + [50.0, 52.5]
    r = relative_return(eq, spy, 60)
    assert r["state"] == PASS, r
    assert abs(r["value"] - 0.10) < 1e-9 and abs(r["benchmark_return"] - 0.05) < 1e-9, r
    assert abs(r["room"] - 0.05) < 1e-9, r
    # book +2%, SPY +5% -> FAIL (lagging the benchmark)
    assert relative_return([100.0] * 59 + [102.0], spy, 60)["state"] == FAIL
    # book DOWN beats a worse SPY on relative terms, but the >= 0 floor still fails it
    r = relative_return([100.0] * 59 + [98.0], [50.0] * 59 + [45.0], 60)
    assert r["state"] == FAIL and "floor" in r["reason"], r
    # too little history -> INSUFFICIENT (22 rows is the real 2026-08-09 state)
    r = relative_return([100.0] * 22, [50.0] * 22, 60)
    assert r["state"] == INSUFFICIENT and "22" in r["reason"], r
    # mismatched series lengths are a data error, not a pass
    assert relative_return([100.0] * 60, [50.0] * 59, 60)["state"] == INSUFFICIENT
    # a non-positive starting value makes the return undefined
    assert relative_return([0.0] * 60, [50.0] * 60, 60)["state"] == INSUFFICIENT
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: FAIL with `NameError: name 'relative_return' is not defined`

- [ ] **Step 3: Implement — add above `_selftest()`**

```python
def relative_return(equity: list[float], benchmark: list[float],
                    window_days: int) -> dict:
    """Criterion 4 (INFORMATIONAL — never gates an order). Total return of the
    marked book against the benchmark over the trailing window, with a floor of
    >= 0 on the book's own return.

    Assumes no deposits or withdrawals across the window: the equity series is a
    pure mark-to-market curve.
    """
    out = {"criterion": "relative_return", "state": INSUFFICIENT, "value": None,
           "benchmark_return": None, "limit": 0.0, "room": None, "reason": ""}
    if len(equity) != len(benchmark):
        out["reason"] = (f"series misaligned: {len(equity)} equity vs "
                         f"{len(benchmark)} benchmark points")
        return out
    if len(equity) < window_days:
        out["reason"] = (f"need {window_days} daily closes, have {len(equity)} "
                         "-- criterion not yet mature")
        return out
    e0, e1 = float(equity[-window_days]), float(equity[-1])
    b0, b1 = float(benchmark[-window_days]), float(benchmark[-1])
    if e0 <= 0 or b0 <= 0:
        out["reason"] = "non-positive starting value; return undefined"
        return out
    er, br = e1 / e0 - 1.0, b1 / b0 - 1.0
    out.update(value=er, benchmark_return=br, room=er - br)
    if er < 0:
        out["state"] = FAIL
        out["reason"] = (f"book {er:+.2%} vs benchmark {br:+.2%} over {window_days}d "
                         "-- below the >= 0 floor")
        return out
    out["state"] = PASS if er >= br else FAIL
    out["reason"] = f"book {er:+.2%} vs benchmark {br:+.2%} over {window_days}d"
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: `selftest OK: mandate loads, terms match`

- [ ] **Step 5: Commit**

```bash
git add src/mandate.py
git commit -m "feat(mandate): criterion 4 -- return vs benchmark, informational

Reports INSUFFICIENT until 60 daily equity points exist; equity.jsonl held 22 on
2026-08-09, so this matures around 2026-10. That is correct and must not be
worked around by shortening the window."
```

---

### Task 6: `status()` — the aggregate the agent and the machine both read

**Files:**
- Modify: `src/mandate.py`

**Interfaces:**
- Produces: `status(equity, benchmark, positions, account_value, outcomes, asof, cfg=None) -> dict`
  returning `{"criteria": {name: {...}}, "blocking_fail": [names], "blocking_unmeasurable": [names], "informational_fail": [names], "tradeable": bool, "degraded": bool}`.
- This is the single entry point Plan 2's `mandate_status` MCP tool and Plan 3's monitor will call. Do not add a second aggregator.

- [ ] **Step 1: Write the failing test — append inside `_selftest()`**

```python
    # --- status(): the aggregate ----------------------------------------------
    healthy = status(equity=[100.0, 101.0], benchmark=[50.0, 50.0],
                     positions={"AAA": {"value": 10.0}}, account_value=100.0,
                     outcomes=[], asof="2026-08-09")
    assert healthy["criteria"]["drawdown"]["state"] == PASS
    assert healthy["criteria"]["concentration"]["state"] == PASS
    # informational criteria are immature today and must NOT block trading
    assert healthy["criteria"]["pnl_concentration"]["state"] == INSUFFICIENT
    assert healthy["criteria"]["relative_return"]["state"] == INSUFFICIENT
    assert healthy["informational_fail"] == []
    assert healthy["blocking_fail"] == [] and healthy["blocking_unmeasurable"] == []
    assert healthy["tradeable"] is True and healthy["degraded"] is False, healthy

    # a blocking FAIL stops trading
    breached = status(equity=[100.0, 80.0], benchmark=[50.0, 50.0],
                      positions={}, account_value=100.0, outcomes=[],
                      asof="2026-08-09")
    assert breached["blocking_fail"] == ["drawdown"], breached
    assert breached["tradeable"] is False

    # a blocking criterion that cannot be MEASURED means degraded mode, not a pass
    dark = status(equity=[100.0, 99.0], benchmark=[50.0, 50.0],
                  positions={"AAA": {"value": None}}, account_value=100.0,
                  outcomes=[], asof="2026-08-09")
    assert dark["blocking_unmeasurable"] == ["concentration"], dark
    assert dark["tradeable"] is False and dark["degraded"] is True, dark
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: FAIL with `NameError: name 'status' is not defined`

- [ ] **Step 3: Implement — add above `_selftest()`**

```python
BLOCKING = ("drawdown", "concentration")
INFORMATIONAL = ("pnl_concentration", "relative_return")


def status(equity: list[float], benchmark: list[float], positions: dict,
           account_value: float, outcomes: list[dict], asof: str,
           cfg: dict | None = None) -> dict:
    """Evaluate all four criteria. THE single entry point — the MCP tool, the
    monitor and the dashboard all read this, so there is exactly one answer to
    "are we passing".

    `tradeable` is False when a BLOCKING criterion fails or cannot be measured.
    Informational criteria never affect it: they judge whether autonomy is
    working, not whether an order is safe.
    """
    m = cfg or load()
    crit = {
        "drawdown": drawdown(equity, m["drawdown"]["max_pct"]),
        "concentration": concentration(positions, account_value,
                                       m["concentration"]["max_position_pct"]),
        "pnl_concentration": pnl_concentration(
            outcomes, asof, m["pnl_concentration"]["window_days"],
            m["pnl_concentration"]["max_single_share"],
            m["pnl_concentration"]["min_distinct_names"]),
        "relative_return": relative_return(
            equity, benchmark, m["relative_return"]["window_days"]),
    }
    b_fail = [k for k in BLOCKING if crit[k]["state"] == FAIL]
    b_dark = [k for k in BLOCKING if crit[k]["state"] == INSUFFICIENT]
    i_fail = [k for k in INFORMATIONAL if crit[k]["state"] == FAIL]
    return {"asof": asof, "criteria": crit,
            "blocking_fail": b_fail, "blocking_unmeasurable": b_dark,
            "informational_fail": i_fail,
            "tradeable": not (b_fail or b_dark),
            "degraded": bool(b_dark)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/mandate.py --selftest`
Expected: `selftest OK: mandate loads, terms match`

- [ ] **Step 5: Update the selftest banner and commit**

Change the final print to:
```python
    print("selftest OK: mandate -- 4 criteria three-state, blocking vs "
          "informational, unmeasurable never passes")
```

```bash
git add src/mandate.py
git commit -m "feat(mandate): status() -- the single aggregate both sides read

Blocking criteria (drawdown, concentration) gate trading; informational ones
(P&L concentration, relative return) never do, so two immature criteria cannot
freeze the book. A blocking criterion that cannot be MEASURED yields degraded
mode, never a pass."
```

---

### Task 7: `assert_agentic_account()` — move account scoping from prose to code

**Files:**
- Modify: `src/governance.py`

**Interfaces:**
- Produces: `governance.assert_agentic_account(accounts: list[dict], snapshot_account: str | None = None) -> str`
  returning the resolved account number, raising `PermissionError` otherwise.
- Plan 2's `place_buy` / `place_sell` MCP tools call this before every order.

**Context:** today this rule lives in `prompts/fast_loop.md` step 2 as an instruction the agent is asked to follow. The reference system gets its Layer 1 from a paper endpoint that physically cannot reach real money; we have no equivalent, so this guard **is** our Layer 1 and it must be code.

- [ ] **Step 1: Write the failing test — add to `_selftest()` in `src/governance.py`, inside the `with tempfile...` block**

```python
            # --- account scoping: OUR layer 1, since no paper endpoint exists ---
            accts = [{"account_number": "111", "agentic_allowed": False},
                     {"account_number": "222", "agentic_allowed": True}]
            assert assert_agentic_account(accts) == "222"
            # zero authorised accounts must raise, never fall through
            try:
                assert_agentic_account([{"account_number": "111",
                                         "agentic_allowed": False}])
                raise AssertionError("must raise when no account is authorised")
            except PermissionError as e:
                assert "no account" in str(e).lower(), e
            # MORE than one is ambiguous and must raise rather than pick
            try:
                assert_agentic_account([{"account_number": "1", "agentic_allowed": True},
                                        {"account_number": "2", "agentic_allowed": True}])
                raise AssertionError("must raise on ambiguity")
            except PermissionError as e:
                assert "exactly one" in str(e).lower(), e
            # a snapshot naming a DIFFERENT account must raise
            try:
                assert_agentic_account(accts, snapshot_account="111")
                raise AssertionError("must raise on account mismatch")
            except PermissionError as e:
                assert "mismatch" in str(e).lower(), e
            # matching snapshot is fine
            assert assert_agentic_account(accts, snapshot_account="222") == "222"
            # empty/garbage input raises rather than returning something falsy
            for bad in ([], None):
                try:
                    assert_agentic_account(bad)
                    raise AssertionError("must raise on empty account list")
                except PermissionError:
                    pass
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/governance.py --selftest`
Expected: FAIL with `NameError: name 'assert_agentic_account' is not defined`

- [ ] **Step 3: Implement — add to `src/governance.py` above `gates()`**

```python
def assert_agentic_account(accounts, snapshot_account: str | None = None) -> str:
    """Resolve THE one tradeable account, or raise. This is Layer 1.

    The reference system at /opt/trading gets its Layer 1 from the endpoint: a
    paper key that cannot reach real money at all. We have no equivalent — the
    same token reaches every Robinhood account — so this guard is the boundary,
    and it must be code rather than an instruction in a prompt.

    Raises PermissionError on: no authorised account, more than one, or a
    mismatch against the account a snapshot was taken from. Never guesses.
    """
    allowed = [a for a in (accounts or [])
               if a.get("agentic_allowed") is True and a.get("account_number")]
    if not allowed:
        raise PermissionError(
            "no account with agentic_allowed=true; placing nothing")
    if len(allowed) > 1:
        raise PermissionError(
            f"expected exactly one agentic_allowed account, found {len(allowed)}: "
            f"{sorted(a['account_number'] for a in allowed)}; placing nothing")
    acct = str(allowed[0]["account_number"])
    if snapshot_account is not None and str(snapshot_account) != acct:
        raise PermissionError(
            f"account mismatch: snapshot is from {snapshot_account!r} but the "
            f"authorised account is {acct!r}; placing nothing")
    return acct
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/governance.py --selftest`
Expected: `selftest OK: governance two-tier gates ...`

- [ ] **Step 5: Commit**

```bash
git add src/governance.py
git commit -m "feat(governance): account scoping becomes code, not prompt prose

/opt/trading's layer 1 is the endpoint -- a paper key that cannot reach real
money. We have no equivalent, so this guard IS our layer 1 and cannot live in
prompts/fast_loop.md step 2 as an instruction the agent is asked to follow.
Raises on zero, on ambiguity, and on snapshot mismatch. Never guesses."
```

---

### Task 8: Liquidity gate — express the constraint, not the list

**Files:**
- Modify: `src/governance.py`
- Modify: `config/mandate.toml`

**Interfaces:**
- Produces: `governance.liquidity_ok(symbol: str, dollar_volume: float | None, min_dollar_volume: float) -> tuple[bool, str]`.

**Context:** `whitelist()` currently restricts *what may be traded*, which is a selection restriction sitting in a safety path — the thing the spec removes. The property it actually encodes is liquidity. This task adds the real constraint. `whitelist()` itself is **not** deleted here; Plan 2 removes its use from the order path once the agent has a tool surface. Deleting it now would leave the current live path with no universe check at all.

- [ ] **Step 1: Add the threshold to `config/mandate.toml`**

```toml
# --- Tradeability floor (safety, not selection) ------------------------------
# The universe whitelist encoded "these names are liquid enough". This states the
# property directly, so the agent may trade anything that clears it rather than
# only names on a list someone curated. Derived from the 20th percentile of
# 20-day average dollar volume across config/universe.csv on 2026-08-09.
[liquidity]
min_dollar_volume_20d = 50_000_000.0
```

- [ ] **Step 2: Write the failing test — add to `governance._selftest()` inside the tempdir block**

```python
            # --- liquidity: the property the whitelist was standing in for ---
            ok, why = liquidity_ok("AAA", 100_000_000.0, 50_000_000.0)
            assert ok and why == "", (ok, why)
            ok, why = liquidity_ok("BBB", 10_000_000.0, 50_000_000.0)
            assert not ok and "below the" in why, (ok, why)
            # exactly at the floor is acceptable
            assert liquidity_ok("CCC", 50_000_000.0, 50_000_000.0)[0]
            # UNKNOWN liquidity fails CLOSED -- never assume a name is tradeable
            ok, why = liquidity_ok("DDD", None, 50_000_000.0)
            assert not ok and "unknown" in why.lower(), (ok, why)
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python src/governance.py --selftest`
Expected: FAIL with `NameError: name 'liquidity_ok' is not defined`

- [ ] **Step 4: Implement — add to `src/governance.py` above `vet_plan()`**

```python
def liquidity_ok(symbol: str, dollar_volume: float | None,
                 min_dollar_volume: float) -> tuple[bool, str]:
    """Tradeability floor. Returns (ok, reason_if_not).

    This is the property config/universe.csv was standing in for. Stating it
    directly means the gate constrains SAFETY without constraining SELECTION —
    the agent may trade anything that clears the floor.

    FAILS CLOSED on unknown liquidity: a name we cannot measure is not a name we
    have shown to be tradeable.
    """
    if dollar_volume is None or not isinstance(dollar_volume, (int, float)):
        return False, f"{symbol}: liquidity unknown; cannot show it clears the floor"
    if float(dollar_volume) < float(min_dollar_volume):
        return False, (f"{symbol}: 20d $-volume {float(dollar_volume):,.0f} is below "
                       f"the {float(min_dollar_volume):,.0f} floor")
    return True, ""
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/python src/governance.py --selftest`
Expected: `selftest OK: governance two-tier gates ...`

- [ ] **Step 6: Commit**

```bash
git add src/governance.py config/mandate.toml
git commit -m "feat(governance): liquidity floor -- the constraint the whitelist encoded

A name whitelist restricts WHAT may be traded, which is selection sitting in a
safety path. This states the actual property, so the gate constrains safety
without constraining choice. Fails closed on unknown liquidity. whitelist() is
untouched here -- Plan 2 removes it from the order path once a tool surface
exists; removing it now would leave the live path with no universe check."
```

---

### Task 9: Mandate-breach flatten decision

**Files:**
- Modify: `src/governance.py`

**Interfaces:**
- Consumes: `mandate.status()` output from Task 6.
- Produces: `governance.mandate_action(status: dict) -> dict` returning
  `{"flatten": bool, "block_entries": bool, "reasons": [str]}`.

**Context:** this is the **only** mechanical close in the system. It decides; it does not execute. Plan 3 wires it to the monitor.

- [ ] **Step 1: Write the failing test — add to `governance._selftest()`**

```python
            # --- mandate action: the ONLY mechanical close in the system ------
            clean = {"blocking_fail": [], "blocking_unmeasurable": [],
                     "criteria": {}, "tradeable": True, "degraded": False}
            a = mandate_action(clean)
            assert a == {"flatten": False, "block_entries": False, "reasons": []}, a
            # a drawdown breach flattens
            dd = {"blocking_fail": ["drawdown"], "blocking_unmeasurable": [],
                  "criteria": {"drawdown": {"reason": "-18% from peak"}},
                  "tradeable": False, "degraded": False}
            a = mandate_action(dd)
            assert a["flatten"] is True and a["block_entries"] is True, a
            assert any("-18%" in r for r in a["reasons"]), a
            # a CONCENTRATION breach blocks entries but must NOT flatten the book --
            # forced selling to fix concentration is a trading decision, not enforcement
            conc = {"blocking_fail": ["concentration"], "blocking_unmeasurable": [],
                    "criteria": {"concentration": {"reason": "AAA at 22%"}},
                    "tradeable": False, "degraded": False}
            a = mandate_action(conc)
            assert a["flatten"] is False and a["block_entries"] is True, a
            # unmeasurable blocking criteria: degraded mode, no new risk, no flatten
            dark = {"blocking_fail": [], "blocking_unmeasurable": ["concentration"],
                    "criteria": {"concentration": {"reason": "no usable mark"}},
                    "tradeable": False, "degraded": True}
            a = mandate_action(dark)
            assert a["flatten"] is False and a["block_entries"] is True, a
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/governance.py --selftest`
Expected: FAIL with `NameError: name 'mandate_action' is not defined`

- [ ] **Step 3: Implement — add to `src/governance.py` above `vet_plan()`**

```python
def mandate_action(status: dict) -> dict:
    """Translate a mandate status into machine action. Decides; does not execute.

    ONLY a drawdown breach flattens — that is the mandate's own terminal term, so
    enforcing it is enforcing the terms rather than forming a market view.

    A concentration breach deliberately does NOT flatten. Choosing which name to
    sell to fix concentration is a trading decision and belongs to the agent; the
    machine blocks new risk and says so loudly.
    """
    reasons, flatten = [], False
    for k in status.get("blocking_fail", []):
        why = (status.get("criteria", {}).get(k) or {}).get("reason", k)
        reasons.append(f"MANDATE BREACH [{k}]: {why}")
        if k == "drawdown":
            flatten = True
    for k in status.get("blocking_unmeasurable", []):
        why = (status.get("criteria", {}).get(k) or {}).get("reason", k)
        reasons.append(f"UNMEASURABLE [{k}]: {why}; degraded mode -- "
                       "manage what is open, open nothing new")
    return {"flatten": flatten,
            "block_entries": bool(reasons),
            "reasons": reasons}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/governance.py --selftest`
Expected: `selftest OK: governance two-tier gates ...`

- [ ] **Step 5: Commit**

```bash
git add src/governance.py
git commit -m "feat(governance): mandate_action -- the only mechanical close

Drawdown breach flattens: that is the mandate's own terminal term, so enforcing
it enforces the terms rather than forming a view. A concentration breach blocks
entries but never flattens -- choosing WHICH name to sell to fix concentration
is a trading decision and belongs to the agent."
```

---

### Task 10: Delete the tautological reward:risk gate

**Files:**
- Modify: `src/research_store/validate.py`

**Context:** `slow_loop.geometry()` builds `targets = entry * (1 + r * stop_dist)` with `r` from `target_r_mults` — so target 1 is *by construction* 2.2× the stop distance and the `>= 2` test can only fail on NaN, rounding, or a degenerate sigma. It carries zero information, has non-zero abort risk (it can reject an entire valid book), and it is a strategy opinion sitting in a safety path. Worse, it *emits a pass*, so it reads to every reader — including the 2026-08-09 repository inspection — as evidence that geometry is validated.

**Verified facts (do not re-derive):** `validate_product(product, mandate=DEFAULT_MANDATE) -> list` returns a **list of error strings** (`[]` means valid) — it is *not* an `(ok, errors)` tuple. It touches only `product.theses`, so a test can pass any object with that attribute. The reward:risk block is `src/research_store/validate.py:76-80`, the helper is `reward_risk()` at line 22, and the threshold is `"min_reward_risk": 2.0` at line 17. `validate.py` has **no** `_selftest` and uses a relative import (`from .models import ...`), so it must be invoked as a package.

- [ ] **Step 1: Write the failing test — add to the bottom of `src/research_store/validate.py`**

```python
def _selftest() -> None:
    """The structural gates must still bite; the tautological one must be gone."""
    import types
    from .models import Thesis, VERDICTS
    v = sorted(VERDICTS)[0]

    def _p(lo, hi, stop, t1, weight=0.05):
        t = Thesis(symbol="AAA", rank=1, verdict=v, target_weight=weight,
                   entry_zone=[lo, hi], stop=stop, targets=[t1], confidence=0.5)
        return types.SimpleNamespace(theses=[t])

    # entry mid 100, risk 2.00, reward 1.50 -> reward:risk 0.75, well under the old
    # 2.0 floor. It must now VALIDATE. The old gate could only ever fail on NaN or a
    # degenerate sigma, because slow_loop built target[0] AS 2.2x the stop distance --
    # arithmetic checking itself, while reading to every reviewer as a real gate.
    assert validate_product(_p(99.0, 101.0, 98.0, 101.5)) == [], \
        validate_product(_p(99.0, 101.0, 98.0, 101.5))

    # the STRUCTURAL gates are unaffected and must still reject:
    assert any("stop" in e for e in validate_product(_p(99.0, 101.0, 100.0, 105.0)))
    assert any("first target" in e for e in validate_product(_p(99.0, 101.0, 98.0, 100.5)))
    assert any("weight" in e for e in validate_product(_p(99.0, 101.0, 98.0, 105.0, 0.5)))
    assert any("entry_zone" in e for e in validate_product(_p(101.0, 99.0, 98.0, 105.0)))

    print("selftest OK: validate -- structural gates bite, tautological R:R gate gone")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from research_store import validate; validate._selftest()"`
Expected: FAIL — `AssertionError` listing `AAA: reward:risk 0.75 < required 2.0`

- [ ] **Step 3: Delete the check**

Delete lines 76–80 (the `rr = reward_risk(t)` block and both `errors.append` branches) and the `"min_reward_risk": 2.0,` entry at line 17. Leave `reward_risk()` itself in place — it is a harmless pure helper and the brief may want to *report* the ratio as a fact. Put this comment where the block was:

```python
    # The reward:risk >= 2 gate was deleted 2026-08-09. It was tautological:
    # slow_loop built target[0] AS 2.2x the stop distance, so it could only fail on
    # NaN, rounding, or a degenerate sigma. Zero information, non-zero abort risk
    # (it could reject a whole valid book), and a STRATEGY opinion inside a SAFETY
    # path. It also emitted a PASS, so it read as evidence geometry was validated.
    # Trade geometry belongs to the agent. See the inversion spec, 2026-08-09.
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from research_store import validate; validate._selftest()"`
Expected: `selftest OK: validate -- structural gates bite, tautological R:R gate gone`

- [ ] **Step 5: Full regression sweep**

```bash
.venv/bin/python src/mandate.py --selftest
.venv/bin/python src/governance.py --selftest
.venv/bin/python src/momentum.py --selftest
.venv/bin/python src/health.py --selftest
.venv/bin/python scripts/record_fills.py --selftest
/usr/bin/python3 scripts/fast_loop.py --selftest
/usr/bin/python3 scripts/market_monitor.py --selftest
python3 src/repo_checks.py
```
Expected: all pass, `repo_checks: PASS (6 checks, repo clean)`

- [ ] **Step 6: Commit**

```bash
git add src/research_store/validate.py
git commit -m "fix(store): delete the tautological reward:risk gate

targets were built AS 2.2x the stop distance, so >= 2 could only fail on NaN or
a degenerate sigma. Zero information, non-zero abort risk, and a strategy opinion
inside a safety path -- and because it emitted a PASS it read as evidence that
geometry was validated. It is not. Geometry belongs to the agent."
```

---

## Done when

- `.venv/bin/python src/mandate.py --selftest` passes
- `.venv/bin/python src/governance.py --selftest` passes
- Every selftest in Task 10 Step 6 passes and `repo_checks` is 6/6
- `mandate.status()` against **live** data returns `tradeable: True`, `degraded: False`, with `drawdown` and `concentration` at `PASS` and both informational criteria at `INSUFFICIENT_DATA`. Verify with:

```bash
.venv/bin/python -c "
import sys, json; sys.path.insert(0,'src')
import mandate, marks, pathlib
eq=[json.loads(l)['value'] for l in
    pathlib.Path('research_store/history/equity.jsonl').read_text().splitlines() if l.strip()]
v=marks.load()
outs=[json.loads(l) for l in
      pathlib.Path('research_store/journal.jsonl').read_text().splitlines() if l.strip()]
s=mandate.status(eq, [0.0]*len(eq), v['positions'], v['account_value'],
                 outs, v['as_of'])
print(json.dumps({k:(c['state'],c['reason']) for k,c in s['criteria'].items()}, indent=2))
print('tradeable', s['tradeable'], '| degraded', s['degraded'])
"
```

**No live trading behaviour changes in this plan.** The mandate is measured but nothing consumes it yet; `mandate_action` and `liquidity_ok` are defined but not wired. Plan 2 (environment) and Plan 3 (enforcement) connect them.
