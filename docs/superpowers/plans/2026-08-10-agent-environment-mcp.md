# Agent Environment (MCP Tool Surface) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the trading agent a custom MCP tool surface so it can SEE the facts and DECIDE, instead of following a procedural markdown script.

**Architecture:** One FastMCP server at `src/agent_env/server.py`, built from small pure helper modules in `src/agent_env/`. Every tool reads state that already exists on disk (`research_store/`, price parquet panels) via modules already in the repo — `marks`, `mandate`, `momentum`, `governance`, `research_store`. Nothing new is invented; the server is a *window* onto facts the system already computes, plus two write tools (`set_levels`, `record_decision`) that record the agent's own decisions.

**Tech Stack:** Python 3.12 (`.venv`), `mcp==1.28.1` (FastMCP), pandas, stdlib. No new dependencies beyond the already-pinned `mcp`.

## Global Constraints

- **Plan 2 of 5** for `docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md`. Read spec §6 (the environment) before starting.
- **The point of this plan is ENABLEMENT, not gating.** The spec's principle: code defines the environment and the guardrails; the agent decides. A tool that answers a question the agent has is worth more than a tool that stops it. Only `check_order()` restricts anything, and even that exists so the agent can *ask* whether something is permitted.
- **This plan changes NO live trading behaviour.** No cron entry runs the server. `scripts/slow_loop.py`, `scripts/fast_loop.py` and `scripts/market_monitor.py` are NOT modified. The live loops keep running exactly as they do today.
- **Runs under `.venv/bin/python` (3.12).** Nothing here imports `moomoo` — live quotes already reach disk via the monitor's `research_store/monitor/quotes.json`. Do not add a moomoo dependency.
- **Testing convention is module-local `--selftest`, NOT pytest.** This repo has no pytest and no `tests/` tree. Every module exposes `_selftest()` with plain `assert` and a `if __name__ == "__main__": if "--selftest" in sys.argv:` block.
- **Gate every commit** on `bash deploy/run_selftests.sh` exiting 0.
- Live money. Never print or commit secrets. Judge risk by mechanism, never by account size.
- Work on `main` unless told otherwise; commit per task.

**Verified interfaces (2026-08-10) — do not guess these:**

```
research_store.read_current() -> ResearchProduct
    .as_of str, .regime dict, .notes, .theses list[Thesis], .by_symbol
Thesis: symbol, rank, verdict, thesis, entry_zone[lo,hi], stop, targets[],
        target_weight, confidence, signals dict, as_of, review_by,
        earnings_date, outcome, decision_id
research_store.store.append_journal(entry: dict) -> None      # src/research_store/store.py:81

marks.load() -> {"account_number","as_of","ts","marked_at","cash","invested",
                 "account_value","positions":{SYM:{"qty","avg_cost","mark","value","pnl"}}}

mandate.status(equity, benchmark, positions, account_value, outcomes, asof, cfg=None)
    -> {"asof","criteria":{name:{...}},"blocking_fail":[],"blocking_unmeasurable":[],
        "informational_fail":[],"tradeable":bool,"degraded":bool}
mandate.load() -> dict          # config/mandate.toml

momentum.compute(panel, asof, lookback=252, residual_tilt=0.0, market=None, factors=None)
    -> DataFrame indexed by ticker: R, sigma, trend, ret, score, eligible, rank
momentum.regime_on(spy: Series, asof, ma_days=50) -> bool

governance.gates(account_value, cfg) -> {"block_all":[], "block_entries":[], "drawdown":f}
governance.vet_plan(plan, account_value, cfg) -> (approved, blocked)
governance.liquidity_ok(symbol, dollar_volume, min_dollar_volume) -> (bool, reason)
governance.assert_agentic_account(accounts, snapshot_account=None) -> str | raises PermissionError

research_store/prices/closes.parquet   2523 rows x 168 cols, index=DatetimeIndex, latest 2026-08-09
                     highs.parquet / lows.parquet / opens.parquet  same shape
config/universe.csv   ticker,source,sector,exchange,flag,as_of   150 rows
config/etf_universe.csv  same first column
research_store/monitor/overrides.json   written atomically by risk_review.py (os.replace),
                                        READ by market_monitor.py:499
research_store/monitor/quotes.json      {"ts":iso,"prices":{SYM:float}}
research_store/history/equity.jsonl     {"date","ts","value","invested","cash"}
```

---

### Task 1: Package skeleton and a server that runs

**Files:**
- Create: `src/agent_env/__init__.py`
- Create: `src/agent_env/server.py`

**Interfaces:**
- Produces: `src/agent_env/server.py` module-level `mcp = FastMCP("agentic-trader")`, a `ping() -> str` tool, and `_selftest()`.

**Note on the package name:** the directory is `src/agent_env/`, which shadows the installed `mcp` SDK package if `src/` is on `sys.path` first. **Verify this immediately in Step 2** — if `from mcp.server.fastmcp import FastMCP` fails from inside `src/agent_env/server.py`, rename the directory to `src/agent_env/` and use that name throughout the rest of the plan. Report which name you used; later tasks depend on it.

- [ ] **Step 1: Create the package**

```bash
mkdir -p src/mcp
```

Create `src/agent_env/__init__.py`:

```python
"""The agent's ENVIRONMENT — the tools it can call to see the book and act on it.

Spec: docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md §6.

This package exists to ENABLE judgment, not to constrain it. Every tool answers a
question the agent might have: what do I hold, how am I doing against the mandate,
what does the screen rank highest, how far does this name actually move. The one
restrictive tool (`check_order`) is here so the agent can ASK whether something is
permitted before trying it.
"""
```

- [ ] **Step 2: Verify the package name does not shadow the SDK**

Run:
```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from mcp.server.fastmcp import FastMCP; print('no shadow')"
```
If this prints `no shadow`, keep `src/agent_env/`. If it raises `ModuleNotFoundError`, `git mv src/mcp src/agent_env` and use `agent_env` everywhere below.

- [ ] **Step 3: Write the server with one tool**

Create `src/agent_env/server.py`:

```python
"""FastMCP server exposing the agent's tool surface.

Run manually:   .venv/bin/python src/agent_env/server.py
Run the tests:  .venv/bin/python src/agent_env/server.py --selftest

Transport is stdio: Claude Code launches this as a subprocess and speaks MCP over
its stdin/stdout, so NOTHING may be printed to stdout except protocol traffic.
Diagnostics go to stderr.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from mcp.server.fastmcp import FastMCP   # noqa: E402

mcp = FastMCP("agentic-trader")


@mcp.tool()
def ping() -> str:
    """Liveness check. Returns 'pong' if the environment server is reachable."""
    return "pong"


def _selftest() -> None:
    assert ping() == "pong"
    assert mcp is not None
    print("selftest OK: mcp server boots, ping responds")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run()
```

- [ ] **Step 4: Run the selftest**

Run: `.venv/bin/python src/agent_env/server.py --selftest`
Expected: `selftest OK: mcp server boots, ping responds`

- [ ] **Step 5: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): MCP server skeleton — the agent's tool surface

Plan 2 of the inversion. This package is a WINDOW onto facts the system already
computes, not new machinery. stdio transport, so stdout carries protocol traffic
only and diagnostics must go to stderr."
```

---

### Task 2: `positions()` and `account()` — what do I hold?

**Files:**
- Create: `src/agent_env/state.py`
- Modify: `src/agent_env/server.py`

**Interfaces:**
- Consumes: `marks.load()`, `research_store.read_current()`.
- Produces: `state.holdings() -> dict`, `state.account_summary() -> dict`; tools `positions()`, `account()`.

- [ ] **Step 1: Write the failing test in `src/agent_env/state.py`**

```python
"""What the agent holds, and what it is worth. Pure assembly over marks.load()
and the current product — no new valuation logic, so the agent, the dashboard and
the equity log can never disagree about what a position is worth."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


def holdings(valued: dict, theses: list) -> dict:
    """Merge broker positions with the levels the agent set for them.

    `valued` is marks.load(). `theses` is product.theses (may be empty).
    A held symbol with no thesis is reported with stop/target None and
    `watched=False` — that is the unprotected case and must be visible, not
    silently dropped.
    """
    raise NotImplementedError


def _selftest() -> None:
    import types
    T = lambda s, stop, tgts: types.SimpleNamespace(
        symbol=s, stop=stop, targets=tgts, target_weight=0.07)
    valued = {"account_value": 100.0, "cash": 10.0, "invested": 90.0,
              "positions": {"AAA": {"qty": 1.0, "avg_cost": 50.0, "mark": 60.0,
                                    "value": 60.0, "pnl": 0.2},
                            "BBB": {"qty": 1.0, "avg_cost": 30.0, "mark": 30.0,
                                    "value": 30.0, "pnl": 0.0}}}
    h = holdings(valued, [T("AAA", 55.0, [70.0, 80.0])])
    assert h["AAA"]["stop"] == 55.0 and h["AAA"]["watched"] is True, h
    assert h["AAA"]["value"] == 60.0 and h["AAA"]["share_of_equity"] == 0.6, h
    # held with NO thesis -> visible, flagged unwatched, never dropped
    assert "BBB" in h and h["BBB"]["stop"] is None and h["BBB"]["watched"] is False, h
    # a thesis for something NOT held must not appear as a holding
    h2 = holdings(valued, [T("ZZZ", 1.0, [2.0])])
    assert "ZZZ" not in h2, h2
    assert holdings({"account_value": 100.0, "positions": {}}, []) == {}
    print("selftest OK: holdings merges marks with agent levels, unwatched visible")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/agent_env/state.py --selftest`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement `holdings()`**

Replace the `raise NotImplementedError` body with:

```python
    by_sym = {t.symbol: t for t in (theses or [])}
    av = float(valued.get("account_value") or 0.0)
    out = {}
    for sym, p in (valued.get("positions") or {}).items():
        t = by_sym.get(sym)
        out[sym] = {
            "qty": p.get("qty"),
            "avg_cost": p.get("avg_cost"),
            "mark": p.get("mark"),
            "value": p.get("value"),
            "pnl": p.get("pnl"),
            "share_of_equity": (float(p["value"]) / av) if av > 0 and p.get("value") is not None else None,
            "stop": getattr(t, "stop", None) if t else None,
            "targets": list(getattr(t, "targets", []) or []) if t else [],
            "watched": bool(t is not None and getattr(t, "stop", None) is not None),
        }
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/agent_env/state.py --selftest`
Expected: `selftest OK: holdings merges marks with agent levels, unwatched visible`

- [ ] **Step 5: Add the two tools to `src/agent_env/server.py`**

Add after the `ping` tool:

```python
import marks                                    # noqa: E402
from research_store import read_current         # noqa: E402
from agent_env import state                           # noqa: E402  sibling module
```

`server.py` already inserts `REPO/src` on `sys.path`, so `from agent_env import state` resolves to `src/agent_env/state.py`. If you renamed the package in Task 1, use `from agent_env import state` instead. Run `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from agent_env import state; print('ok')"` to confirm before continuing.

Then the tools:

```python
@mcp.tool()
def positions() -> str:
    """Every position actually held, with the stop and target set for it.

    `watched: false` means the position has NO stop being enforced — it is
    unprotected. That is deliberately reported rather than hidden.
    """
    prod = read_current()
    return json.dumps(state.holdings(marks.load(),
                                     prod.theses if prod else []), indent=2, default=str)


@mcp.tool()
def account() -> str:
    """Account value, cash, invested capital, and when it was last marked."""
    v = marks.load()
    return json.dumps({k: v.get(k) for k in
                       ("account_number", "account_value", "cash", "invested",
                        "as_of", "marked_at")}, indent=2, default=str)
```

- [ ] **Step 6: Verify both tools against live data**

Run:
```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from agent_env import server
print(server.account())
import json; h=json.loads(server.positions())
print('positions:', len(h), '| unwatched:', [s for s,v in h.items() if not v['watched']])"
```
Expected: a JSON account block, a position count of 13, and a list (possibly empty) of unwatched symbols.

- [ ] **Step 7: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): positions() and account() — what the agent holds

Values through src/marks.py, the one place positions become dollars, so the agent,
the dashboard and the equity log cannot disagree. A held symbol with no stop is
reported with watched=false rather than dropped — the unprotected case must be
visible to the agent that has to deal with it."
```

---

### Task 3: `mandate_status()` — am I passing, and how much room?

**Files:**
- Modify: `src/agent_env/server.py`

**Interfaces:**
- Consumes: `mandate.status(...)`, `marks.load()`, `research_store/history/equity.jsonl`, `research_store/journal.jsonl`, `research_store/prices/closes.parquet` (for the SPY benchmark series).
- Produces: tool `mandate_status()`.

- [ ] **Step 1: Add a helper to `src/agent_env/state.py` with its test**

Append to `state.py` above `_selftest`:

```python
def equity_series(path: Path) -> list:
    """Ordered daily equity closes, oldest first. Skips malformed rows rather
    than raising — mandate.drawdown() applies its own missing-data discipline."""
    import json
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line)["value"])
        except Exception:
            continue
    return out
```

Add to `_selftest()` before the print:

```python
    import tempfile, json as _json
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "equity.jsonl"
        f.write_text('{"date":"2026-08-01","value":100.0}\n'
                     'not json\n'
                     '{"date":"2026-08-02","value":95.0}\n')
        assert equity_series(f) == [100.0, 95.0], equity_series(f)
        assert equity_series(Path(d) / "absent.jsonl") == []
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python src/agent_env/state.py --selftest`
Expected: the selftest OK line (the new assertions pass).

- [ ] **Step 3: Add the tool to `src/agent_env/server.py`**

```python
import mandate                                  # noqa: E402
import pandas as pd                             # noqa: E402

EQUITY = REPO / "research_store" / "history" / "equity.jsonl"
JOURNAL = REPO / "research_store" / "journal.jsonl"
CLOSES = REPO / "research_store" / "prices" / "closes.parquet"


def _outcomes() -> list:
    if not JOURNAL.exists():
        return []
    rows = []
    for line in JOURNAL.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


@mcp.tool()
def mandate_status() -> str:
    """All four mandate criteria with real numbers and the room left on each.

    Blocking criteria (drawdown, concentration) gate trading. Informational ones
    (P&L concentration, relative return) judge whether the approach is working and
    never gate an order. A criterion reporting INSUFFICIENT_DATA is NOT a pass.
    """
    v = marks.load()
    eq = state.equity_series(EQUITY)
    bench = []
    if CLOSES.exists():
        try:
            spy = pd.read_parquet(CLOSES)["SPY"].dropna().tolist()
            bench = spy[-len(eq):] if eq else []
        except Exception:
            bench = []
    if len(bench) != len(eq):
        bench = [0.0] * len(eq)      # misaligned -> criterion 4 reports INSUFFICIENT
    s = mandate.status(eq, bench, v["positions"], v["account_value"],
                       _outcomes(), v["as_of"])
    return json.dumps(s, indent=2, default=str)
```

- [ ] **Step 4: Verify against live data**

Run:
```bash
.venv/bin/python -c "
import sys,json; sys.path.insert(0,'src')
from agent_env import server
s=json.loads(server.mandate_status())
for k,c in s['criteria'].items(): print(f\"  {k:20s} {c['state']}\")
print('  tradeable:', s['tradeable'])"
```
Expected: `drawdown PASS`, `concentration PASS`, both informational `INSUFFICIENT_DATA`, `tradeable: True`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): mandate_status() — the terms, with real numbers

The agent can always ask whether it is passing and how much room is left. Reads
mandate.status(), the single aggregate the monitor and dashboard also use, so
there is exactly one answer in the system to 'are we passing'."
```

---

### Task 4: `candidates()` and `universe()` — the screen as an attention budget

**Files:**
- Create: `src/agent_env/screen.py`
- Modify: `src/agent_env/server.py`

**Interfaces:**
- Consumes: `momentum.compute(panel, asof)`, `config/universe.csv`, `config/etf_universe.csv`, `research_store/prices/closes.parquet`.
- Produces: `screen.rank(panel, asof, universe) -> DataFrame`; tools `candidates(n=10)`, `universe()`.

**Design note the implementer must honour:** the top-N is an **attention budget, not a boundary**. `candidates()` returns the top N *and* states in its docstring that the full list is one `universe()` call away. Neither tool restricts what may be traded.

- [ ] **Step 1: Write the failing test in `src/agent_env/screen.py`**

```python
"""The momentum screen, exposed as a CANDIDATE GENERATOR.

Spec §3: a screen is not a decision. It ranks; the agent chooses — including
choosing nothing, or something outside the top N, with a stated reason. Nothing
here restricts what may be traded.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import momentum   # noqa: E402


def read_universe(path: Path) -> list:
    """First column of a header-carrying CSV, blank lines skipped."""
    raise NotImplementedError


def rank(panel, asof, tickers: list):
    """momentum.compute restricted to `tickers`, sorted best-first.

    Returns the full scored frame — the caller decides how many to show.
    """
    raise NotImplementedError


def _selftest() -> None:
    import tempfile, numpy as np, pandas as pd
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "u.csv"
        p.write_text("ticker,sector\nAAA,tech\nBBB,fin\n\n")
        assert read_universe(p) == ["AAA", "BBB"], read_universe(p)

    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    t = np.arange(n)
    panel = pd.DataFrame({
        "AAA": 100 * np.cumprod(1 + (0.002 + 0.005 * np.sin(2 * np.pi * t / 11))),
        "BBB": 100 * np.cumprod(1 + (0.0005 + 0.005 * np.sin(2 * np.pi * t / 13))),
        "CCC": 100 * np.cumprod(1 + (-0.001 + 0.005 * np.sin(2 * np.pi * t / 7))),
    }, index=idx)
    r = rank(panel, idx[-1], ["AAA", "BBB", "CCC"])
    assert list(r.columns) >= ["R", "sigma", "score"], list(r.columns)
    assert r.index[0] == "AAA", r.index.tolist()          # strongest first
    # restricting the universe must not change the remaining names' own numbers
    r2 = rank(panel, idx[-1], ["AAA", "BBB"])
    assert "CCC" not in r2.index, r2.index.tolist()
    assert abs(r2.loc["AAA", "R"] - r.loc["AAA", "R"]) < 1e-12
    # a ticker absent from the panel is simply not ranked, never an error
    r3 = rank(panel, idx[-1], ["AAA", "NOPE"])
    assert "NOPE" not in r3.index and "AAA" in r3.index
    print("selftest OK: screen ranks, restricts cleanly, tolerates unknown tickers")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/agent_env/screen.py --selftest`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement both functions**

```python
def read_universe(path: Path) -> list:
    return [ln.split(",")[0].strip()
            for ln in path.read_text().splitlines()[1:] if ln.strip()]


def rank(panel, asof, tickers: list):
    cols = [c for c in tickers if c in panel.columns]
    scored = momentum.compute(panel[cols], asof)
    if scored.empty:
        return scored
    return scored.sort_values("score", ascending=False)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/agent_env/screen.py --selftest`
Expected: `selftest OK: screen ranks, restricts cleanly, tolerates unknown tickers`

- [ ] **Step 5: Add the tools to `src/agent_env/server.py`**

```python
from agent_env import screen                          # noqa: E402  (adjust if renamed)

UNIVERSE = REPO / "config" / "universe.csv"
ETF_UNIVERSE = REPO / "config" / "etf_universe.csv"


def _panel():
    return pd.read_parquet(CLOSES)


def _all_tickers() -> list:
    return screen.read_universe(UNIVERSE) + screen.read_universe(ETF_UNIVERSE)


@mcp.tool()
def candidates(n: int = 10) -> str:
    """The momentum screen's top `n` names, strongest first, with their numbers.

    This is an ATTENTION BUDGET, not a boundary — it exists so you are not
    scanning 168 names every session. The full ranked list is one `universe()`
    call away, and you may trade something outside the top n; say why when you do.

    Columns: R (12-month return), sigma (daily volatility), trend (distance above
    the 200-day mean), score (the rank-average), eligible (12-month return > 0).
    """
    panel = _panel()
    r = screen.rank(panel, panel.index[-1], _all_tickers())
    return r.head(int(n)).round(4).to_json(orient="index", indent=2)


@mcp.tool()
def universe() -> str:
    """The FULL ranked list — every name in config/universe.csv plus the ETF
    sleeve, scored and sorted. Call this when the top candidates do not suit and
    you want to see everything available."""
    panel = _panel()
    r = screen.rank(panel, panel.index[-1], _all_tickers())
    return r.round(4).to_json(orient="index", indent=2)
```

- [ ] **Step 6: Verify against live data**

Run:
```bash
.venv/bin/python -c "
import sys,json; sys.path.insert(0,'src')
from agent_env import server
c=json.loads(server.candidates(5)); print('  top 5:', list(c))
u=json.loads(server.universe()); print('  full list size:', len(u))"
```
Expected: five tickers, and a full list of roughly 150–168 names.

- [ ] **Step 7: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): candidates() and universe() — the screen as a candidate generator

Spec §3: a screen ranks, it does not decide. The top-10 is an attention budget so
the agent is not scanning 168 names a session, with the full list one call away.
Neither tool restricts what may be traded."
```

---

### Task 5: `terrain()` — how far does this name actually move?

**Files:**
- Create: `src/agent_env/terrain.py`
- Modify: `src/agent_env/server.py`

**Interfaces:**
- Consumes: `research_store/prices/{closes,highs,lows}.parquet`.
- Produces: `terrain.excursions(close, high, low, symbol, horizons) -> dict`; tool `terrain(symbol)`.

**Why this tool exists:** `scripts/calibrate_geometry.py` (2026-08-09) measured that the old formula placed the first take-profit 5.5 daily sigma away, a distance price reaches roughly 2.6% of the time in five days, while the stop at 2.5 sigma is hit about 20% of the time. The agent sets its own levels now, so it needs the same measurement per name, not a formula.

- [ ] **Step 1: Write the failing test in `src/agent_env/terrain.py`**

```python
"""How far a name actually travels, in units of its own daily volatility.

The agent sets its own stops and targets, so it needs the distribution rather
than a formula. Measured the same way as scripts/calibrate_geometry.py: trailing
252-day daily sigma (matching src/momentum.py), then the forward distribution of
max-high and min-low over each horizon.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

LOOKBACK = 252


def excursions(close, high, low, symbol: str, horizons=(1, 3, 5, 10, 20)) -> dict:
    """Per-horizon favourable/adverse excursion quantiles in sigma units.

    Returns {"symbol","sigma_pct", horizons:{h:{"mfe_median","mfe_p90","mae_median",
    "mae_p10","n"}}} or {"error": ...} when the name has too little history.
    """
    raise NotImplementedError


def _selftest() -> None:
    import pandas as pd
    n = 400
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(0)
    px = 100 * np.cumprod(1 + rng.normal(0.0005, 0.02, n))
    close = pd.DataFrame({"AAA": px}, index=idx)
    high = pd.DataFrame({"AAA": px * 1.01}, index=idx)
    low = pd.DataFrame({"AAA": px * 0.99}, index=idx)

    t = excursions(close, high, low, "AAA")
    assert t["symbol"] == "AAA" and t["sigma_pct"] > 0, t
    assert set(t["horizons"]) == {1, 3, 5, 10, 20}, t["horizons"].keys()
    h5 = t["horizons"][5]
    assert h5["n"] > 0 and h5["mfe_median"] > 0 > h5["mae_median"], h5
    # favourable excursion must grow with the horizon
    assert t["horizons"][20]["mfe_median"] > t["horizons"][1]["mfe_median"], t
    # an unknown symbol is an explained error, never a crash or a fake number
    assert "error" in excursions(close, high, low, "NOPE")
    # too little history is also an explained error
    short = close.iloc[:10]
    assert "error" in excursions(short, high.iloc[:10], low.iloc[:10], "AAA")
    print("selftest OK: terrain excursions scale with horizon, unknown symbol explained")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/agent_env/terrain.py --selftest`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement `excursions()`**

```python
def excursions(close, high, low, symbol: str, horizons=(1, 3, 5, 10, 20)) -> dict:
    if symbol not in close.columns:
        return {"error": f"{symbol} is not in the price panel"}
    c = close[symbol].dropna()
    if len(c) < LOOKBACK + max(horizons) + 1:
        return {"error": f"{symbol} has {len(c)} closes; need "
                         f"{LOOKBACK + max(horizons) + 1} for a 252d sigma plus a "
                         f"{max(horizons)}-day forward window"}
    h = high[symbol].reindex(c.index)
    l = low[symbol].reindex(c.index)
    sigma = c.pct_change().rolling(LOOKBACK).std()

    out = {"symbol": symbol, "sigma_pct": float(sigma.iloc[-1] * 100.0), "horizons": {}}
    for n in horizons:
        fwd_max = h[::-1].rolling(n, min_periods=n).max()[::-1].shift(-1)
        fwd_min = l[::-1].rolling(n, min_periods=n).min()[::-1].shift(-1)
        mfe = ((fwd_max / c - 1.0) / sigma).replace([np.inf, -np.inf], np.nan).dropna()
        mae = ((fwd_min / c - 1.0) / sigma).replace([np.inf, -np.inf], np.nan).dropna()
        if mfe.empty or mae.empty:
            continue
        out["horizons"][n] = {
            "n": int(len(mfe)),
            "mfe_median": round(float(mfe.median()), 3),
            "mfe_p90": round(float(mfe.quantile(0.90)), 3),
            "mae_median": round(float(mae.median()), 3),
            "mae_p10": round(float(mae.quantile(0.10)), 3),
        }
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/agent_env/terrain.py --selftest`
Expected: `selftest OK: terrain excursions scale with horizon, unknown symbol explained`

- [ ] **Step 5: Add the tool to `src/agent_env/server.py`**

```python
from agent_env import terrain as terrain_mod          # noqa: E402  (adjust if renamed)

HIGHS = REPO / "research_store" / "prices" / "highs.parquet"
LOWS = REPO / "research_store" / "prices" / "lows.parquet"


@mcp.tool()
def terrain(symbol: str) -> str:
    """How far this name actually travels, in units of its own daily volatility.

    Use this to set stops and targets against measured behaviour rather than a
    formula. `mfe_median` at horizon 5 is the typical BEST move over five sessions
    in sigma; `mae_median` the typical worst. A target beyond `mfe_p90` is reached
    less than one time in ten.

    Context: the retired formula placed the first target 5.5 sigma out, which
    price reached about 2.6% of the time in five days, while its 2.5-sigma stop
    was hit about 20% of the time (scripts/calibrate_geometry.py, 2026-08-09).
    """
    return json.dumps(terrain_mod.excursions(
        pd.read_parquet(CLOSES), pd.read_parquet(HIGHS), pd.read_parquet(LOWS),
        symbol.strip().upper()), indent=2, default=str)
```

- [ ] **Step 6: Verify against live data**

Run:
```bash
.venv/bin/python -c "
import sys,json; sys.path.insert(0,'src')
from agent_env import server
t=json.loads(server.terrain('MU'))
print('  MU sigma%:', round(t['sigma_pct'],2))
print('  5d mfe_median:', t['horizons']['5']['mfe_median'], 'mae_median:', t['horizons']['5']['mae_median'])
print('  unknown:', json.loads(server.terrain('NOPE')).get('error','')[:40])"
```
Expected: a positive sigma, a positive 5-day median favourable excursion, a negative adverse one, and an explained error for the unknown symbol.

- [ ] **Step 7: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): terrain() — measured excursions, so levels beat formulas

The agent sets its own stops and targets now, so it needs the distribution price
actually moves in, per name. Same math as scripts/calibrate_geometry.py, which
found the retired formula's first target sat 5.5 sigma out — reached ~2.6% of the
time in five days against a stop hit ~20% of the time."
```

---

### Task 6: `set_levels()` — the agent's own stop and target

**Files:**
- Create: `src/agent_env/decide.py`
- Modify: `src/agent_env/server.py`

**Interfaces:**
- Consumes: `research_store/monitor/overrides.json` (the file `scripts/risk_review.py` writes and `scripts/market_monitor.py:499` reads).
- Produces: `decide.merge_levels(existing, symbol, stop, target, reason, ts) -> dict`; tool `set_levels(symbol, stop, target, reason)`.

**Why this file:** `overrides.json` is already written atomically by `risk_review.py` (via `os.replace`) and already read by the monitor, so the agent's levels flow through a proven path rather than new plumbing. **Note for the implementer:** the monitor currently applies overrides *stricter-only*. Loosening that so the agent's level is honoured in both directions is Plan 3's job — do NOT change the monitor here.

- [ ] **Step 1: Write the failing test in `src/agent_env/decide.py`**

```python
"""Where the agent records the decisions it makes: the levels it sets, and why.

`reason` is mandatory on every write. A level with no stated reason cannot be
reviewed later, and a later session cannot tell whether the thesis behind it
still holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


def merge_levels(existing: dict, symbol: str, stop, target, reason: str, ts: str) -> dict:
    """Return a NEW overrides dict with `symbol` set. Never mutates `existing`.

    Raises ValueError on: a blank reason, a non-finite or non-positive level, or
    a target at or below the stop (which would be immediately self-triggering).
    """
    raise NotImplementedError


def _selftest() -> None:
    base = {"AAA": {"stop": 10.0, "target": 20.0, "reason": "old", "ts": "t0"}}
    out = merge_levels(base, "BBB", 5.0, 9.0, "broke out on volume", "t1")
    assert out["BBB"]["stop"] == 5.0 and out["BBB"]["target"] == 9.0
    assert out["BBB"]["reason"] == "broke out on volume" and out["BBB"]["ts"] == "t1"
    assert base == {"AAA": {"stop": 10.0, "target": 20.0, "reason": "old", "ts": "t0"}}, "mutated input"
    assert out["AAA"] == base["AAA"], "clobbered an unrelated symbol"
    # overwriting the same symbol replaces it
    out2 = merge_levels(out, "AAA", 11.0, 21.0, "tightened", "t2")
    assert out2["AAA"]["stop"] == 11.0 and out2["AAA"]["reason"] == "tightened"
    # a stop with no target is allowed (target None)
    assert merge_levels({}, "CCC", 5.0, None, "stop only", "t")["CCC"]["target"] is None

    for bad, why in [
        (("DDD", 5.0, 9.0, "", "t"), "blank reason"),
        (("DDD", 5.0, 9.0, "   ", "t"), "whitespace reason"),
        (("DDD", float("nan"), 9.0, "r", "t"), "non-finite stop"),
        (("DDD", 0.0, 9.0, "r", "t"), "non-positive stop"),
        (("DDD", -1.0, 9.0, "r", "t"), "negative stop"),
        (("DDD", 5.0, 5.0, "r", "t"), "target equal to stop"),
        (("DDD", 5.0, 4.0, "r", "t"), "target below stop"),
        (("DDD", 5.0, float("inf"), "r", "t"), "non-finite target"),
    ]:
        try:
            merge_levels({}, *bad)
            raise AssertionError(f"should have rejected: {why}")
        except ValueError:
            pass
    print("selftest OK: merge_levels is pure, reason mandatory, levels sane")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement `merge_levels()`**

```python
import math


def merge_levels(existing: dict, symbol: str, stop, target, reason: str, ts: str) -> dict:
    if not reason or not str(reason).strip():
        raise ValueError("reason is required: a level nobody can review is a level "
                         "a later session cannot judge")
    try:
        s = float(stop)
    except (TypeError, ValueError):
        raise ValueError(f"stop {stop!r} is not a number")
    if not math.isfinite(s) or s <= 0:
        raise ValueError(f"stop {stop!r} must be a finite positive price")
    t = None
    if target is not None:
        try:
            t = float(target)
        except (TypeError, ValueError):
            raise ValueError(f"target {target!r} is not a number")
        if not math.isfinite(t) or t <= 0:
            raise ValueError(f"target {target!r} must be a finite positive price")
        if t <= s:
            raise ValueError(f"target {t} is at or below stop {s}; it would trigger "
                             "immediately")
    out = {k: dict(v) for k, v in (existing or {}).items()}
    out[str(symbol).strip().upper()] = {"stop": s, "target": t,
                                        "reason": str(reason).strip(), "ts": ts}
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: `selftest OK: merge_levels is pure, reason mandatory, levels sane`

- [ ] **Step 5: Add an atomic writer and the tool**

Append to `decide.py`:

```python
OVERRIDES = REPO / "research_store" / "monitor" / "overrides.json"


def write_levels(symbol: str, stop, target, reason: str, ts: str,
                 path: Path = OVERRIDES) -> dict:
    """Merge one symbol's levels into overrides.json ATOMICALLY.

    os.replace mirrors scripts/risk_review.py: the monitor reads this file every
    poll and a torn read makes it drop ALL overrides for that tick.
    """
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = {}
    merged = merge_levels(existing, symbol, stop, target, reason, ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2))
    os.replace(tmp, path)
    return merged
```

In `src/agent_env/server.py`:

```python
from datetime import datetime, timezone         # noqa: E402
from agent_env import decide                          # noqa: E402  (adjust if renamed)


@mcp.tool()
def set_levels(symbol: str, stop: float, target: float = 0.0,
               reason: str = "") -> str:
    """Set YOUR stop and take-profit for a position. `reason` is required.

    The monitor enforces exactly what you set here — this is how a position gets
    protected. Pass target=0 to set a stop with no take-profit. Use `terrain()`
    first so the levels sit where price actually goes.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        merged = decide.write_levels(symbol, stop, target or None, reason, ts)
    except ValueError as e:
        return json.dumps({"ok": False, "error": str(e)}, indent=2)
    return json.dumps({"ok": True, "symbol": symbol.strip().upper(),
                       "levels": merged[symbol.strip().upper()]}, indent=2)
```

- [ ] **Step 6: Verify the tool round-trips without disturbing live state**

Run:
```bash
cp research_store/monitor/overrides.json /tmp/ov.bak 2>/dev/null || echo "{}" > /tmp/ov.bak
.venv/bin/python -c "
import sys,json; sys.path.insert(0,'src')
from agent_env import server
print(server.set_levels('ZZTEST', 10.0, 20.0, 'plan-2 smoke test'))
print(server.set_levels('ZZTEST', 10.0, 5.0, 'should reject'))
print(server.set_levels('ZZTEST', 10.0, 20.0, ''))"
cp /tmp/ov.bak research_store/monitor/overrides.json 2>/dev/null || rm -f research_store/monitor/overrides.json
echo "restored overrides.json"
```
Expected: the first call `ok: true`; the second rejected for a target below the stop; the third rejected for a blank reason. **The restore line is mandatory** — the monitor reads this file live.

- [ ] **Step 7: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): set_levels() — the agent's own stop and target, with a reason

Writes research_store/monitor/overrides.json, the file risk_review already writes
atomically and the monitor already reads, so the agent's levels travel a proven
path rather than new plumbing. reason is mandatory: a level nobody can review is
one a later session cannot judge. The monitor still applies overrides
stricter-only — honouring the agent's level in both directions is Plan 3."
```

---

### Task 7: `record_decision()` — every action carries a why

**Files:**
- Modify: `src/agent_env/decide.py`, `src/agent_env/server.py`

**Interfaces:**
- Consumes: `research_store.store.append_journal(entry: dict)`.
- Produces: `decide.decision_entry(symbol, action, reason, ts) -> dict`; tool `record_decision(symbol, action, reason)`.

- [ ] **Step 1: Write the failing test — append to `decide.py`'s `_selftest()`**

```python
    e = decision_entry("aaa", "OPEN", "strongest score, terrain supports a 3s target", "t1")
    assert e["event"] == "agent_decision" and e["symbol"] == "AAA", e
    assert e["action"] == "open" and e["ts"] == "t1", e
    assert e["reason"].startswith("strongest score"), e
    for bad in [("AAA", "open", ""), ("AAA", "", "r"), ("", "open", "r")]:
        try:
            decision_entry(*bad, "t")
            raise AssertionError(f"should have rejected {bad}")
        except ValueError:
            pass
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: FAIL with `NameError: name 'decision_entry' is not defined`

- [ ] **Step 3: Implement it in `decide.py`**

```python
def decision_entry(symbol: str, action: str, reason: str, ts: str) -> dict:
    """Build the journal event for one agent decision. Pure.

    Every field is required. An action with no reason is exactly the thing that
    makes a later review impossible.
    """
    sym = str(symbol or "").strip().upper()
    act = str(action or "").strip().lower()
    why = str(reason or "").strip()
    if not sym:
        raise ValueError("symbol is required")
    if not act:
        raise ValueError("action is required")
    if not why:
        raise ValueError("reason is required: an action with no stated why cannot "
                         "be reviewed")
    return {"event": "agent_decision", "ts": ts, "symbol": sym,
            "action": act, "reason": why}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: `selftest OK: merge_levels is pure, reason mandatory, levels sane`

- [ ] **Step 5: Add the tool to `src/agent_env/server.py`**

```python
from research_store import store                # noqa: E402


@mcp.tool()
def record_decision(symbol: str, action: str, reason: str) -> str:
    """Record a decision and WHY, to the append-only journal.

    Call this for anything you decide, including deciding NOT to act — a
    considered pass is a decision, and a later session cannot tell the difference
    between 'ruled this out' and 'never looked' unless you say so.

    action is free text: open, add, trim, exit, hold, skip, tighten_stop, …
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        entry = decide.decision_entry(symbol, action, reason, ts)
    except ValueError as e:
        return json.dumps({"ok": False, "error": str(e)}, indent=2)
    store.append_journal(entry)
    return json.dumps({"ok": True, "recorded": entry}, indent=2)
```

- [ ] **Step 6: Verify it writes exactly one journal line**

Run:
```bash
BEFORE=$(wc -l < research_store/journal.jsonl)
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from agent_env import server
print(server.record_decision('ZZTEST','skip','plan-2 smoke test — not a real decision'))
print(server.record_decision('ZZTEST','skip',''))"
AFTER=$(wc -l < research_store/journal.jsonl)
echo "journal lines: $BEFORE -> $AFTER (expect exactly +1; the blank-reason call must be rejected)"
tail -1 research_store/journal.jsonl
```
Expected: exactly one new line, and the second call rejected. The test line stays in the journal — it is append-only by design and a `ZZTEST` skip is harmless and honest.

- [ ] **Step 7: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): record_decision() — every action carries a why

Including deciding NOT to act. A later session cannot distinguish 'ruled this out'
from 'never looked at it' unless the pass was recorded, and that distinction is
what stops an autonomous agent rediscovering the same ground every session."
```

---

### Task 8: `check_order()` — can the agent ask whether this is permitted?

**Files:**
- Modify: `src/agent_env/server.py`

**Interfaces:**
- Consumes: `governance.gates()`, `governance.vet_plan()`, `governance.liquidity_ok()`, `strategy.load()`, `marks.load()`.
- Produces: tool `check_order(symbol, side, amount)`.

**Framing the implementer must honour:** this tool exists so the agent can ASK. It is the one restrictive surface in this plan, and it is a question-answering tool first: it returns a clear allowed/refused with a reason the agent can act on or report. It does not place anything — Robinhood is MCP-only, so placement happens through the broker's own MCP tools.

- [ ] **Step 1: Add the tool**

```python
import governance as gov                        # noqa: E402
import strategy as strat                        # noqa: E402


@mcp.tool()
def check_order(symbol: str, side: str, amount: float) -> str:
    """Ask whether an order is permitted BEFORE you place it.

    Returns {"allowed": bool, "reasons": [...]}. Call this first, then place
    through the Robinhood MCP tools if allowed. A refusal is a finding worth
    reporting, never something to work around.

    Checks: the kill switch and halt-entries switches, the drawdown halt,
    concentration, per-order size, and the liquidity floor. It does NOT restrict
    which name you pick beyond that floor — symbol selection is yours.
    """
    cfg = strat.load()
    v = marks.load()
    acct = float(v["account_value"])
    sym = str(symbol).strip().upper()
    sd = str(side).strip().lower()
    reasons = []

    g = gov.gates(acct, cfg)
    reasons += g["block_all"]
    if sd == "buy":
        reasons += g["block_entries"]

    approved, blocked = gov.vet_plan(
        [{"symbol": sym, "side": sd, "amount": float(amount)}], acct, cfg)
    reasons += [b["blocked"] for b in blocked]

    if sd == "buy":
        floor = float(mandate.load().get("liquidity", {})
                      .get("min_dollar_volume_20d", 0.0)) or float(
                          cfg.get("governance", {}).get("min_dollar_volume_20d", 0.0))
        if floor > 0:
            ok, why = gov.liquidity_ok(sym, _dollar_volume(sym), floor)
            if not ok:
                reasons.append(why)

    return json.dumps({"allowed": not reasons, "symbol": sym, "side": sd,
                       "amount": float(amount), "reasons": reasons}, indent=2)
```

Add the helper above it:

```python
def _dollar_volume(symbol: str):
    """Trailing 20-day average dollar volume, or None if not measurable.

    Returns None rather than a guess: governance.liquidity_ok fails CLOSED on an
    unknown value, which is the correct direction — a name we cannot measure has
    not been shown to be tradeable.
    """
    dvol = REPO / "research_store" / "prices" / "pool_dvol.parquet"
    if not dvol.exists():
        return None
    try:
        d = pd.read_parquet(dvol)
        if symbol not in d.columns:
            return None
        s = d[symbol].dropna().tail(20)
        return float(s.mean()) if len(s) else None
    except Exception:
        return None
```

- [ ] **Step 2: Verify against live state**

Run:
```bash
.venv/bin/python -c "
import sys,json; sys.path.insert(0,'src')
from agent_env import server
for sym,side,amt in [('MU','buy',5.0),('MU','sell',5.0),('ZZZZ','buy',5.0),('MU','buy',999999.0)]:
    r=json.loads(server.check_order(sym,side,amt))
    print(f'  {sym:6s} {side:5s} {amt:>9}  allowed={r[\"allowed\"]}  {(r[\"reasons\"] or [\"\"])[0][:52]}')"
```
Expected: a normal buy allowed; a sell allowed; an off-universe or unmeasurable name refused on liquidity; an oversized buy refused on the per-order cap.

- [ ] **Step 3: Confirm a SELL is never refused for a buy-only reason**

Run:
```bash
.venv/bin/python -c "
import sys,json; sys.path.insert(0,'src')
from agent_env import server
r=json.loads(server.check_order('ZZZZ_NOT_IN_UNIVERSE','sell',5.0))
print('  off-universe SELL allowed:', r['allowed'], '(must be True — never strand an exit)')"
```
Expected: `True`. Stops are software-only; refusing an exit removes a position's only protection.

- [ ] **Step 4: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): check_order() — the agent can ask what is permitted

A question-answering tool first: returns allowed/refused with a reason the agent
can act on or report. It does not place — Robinhood is MCP-only, so placement is
the agent's own broker call. It constrains SAFETY (switches, drawdown,
concentration, size, liquidity) and never selection: symbol choice is the agent's."
```

---

### Task 9: `brief()` — the whole picture in one call

**Files:**
- Modify: `src/agent_env/server.py`

**Interfaces:**
- Consumes: every tool above, plus `momentum.regime_on()` and `research_store.read_current()`.
- Produces: tool `brief()`.

**The brief is FACTS, never instructions.** Regime is reported as an observation ("SPY is above its 50-day mean"), not as a switch that decides anything. If any line of the output reads as an instruction about what to trade, that is a defect.

- [ ] **Step 1: Add the tool**

```python
@mcp.tool()
def brief() -> str:
    """Everything you need to decide, assembled fresh: mandate status, what you
    hold and whether it is protected, the top candidates, and the market backdrop.

    These are FACTS, not instructions. The regime line is an observation, not a
    switch — nothing here decides for you. Pull `terrain(symbol)` before setting
    levels, and `universe()` if the top candidates do not suit.
    """
    prod = read_current()
    v = marks.load()
    panel = _panel()
    asof = panel.index[-1]

    regime = None
    if "SPY" in panel.columns:
        try:
            regime = {
                "spy_above_50dma": bool(momentum.regime_on(panel["SPY"], asof)),
                "note": "an observation about the market, not a rule that acts",
            }
        except Exception:
            regime = None

    held = state.holdings(v, prod.theses if prod else [])
    top = screen.rank(panel, asof, _all_tickers()).head(10).round(4)

    return json.dumps({
        "as_of": str(asof.date()),
        "book_as_of": prod.as_of if prod else None,
        "account": {k: v.get(k) for k in ("account_value", "cash", "invested")},
        "mandate": json.loads(mandate_status()),
        "positions": held,
        "unprotected": [s for s, h in held.items() if not h["watched"]],
        "candidates": json.loads(top.to_json(orient="index")),
        "regime": regime,
        "available": "candidates() shows 10 of ~168; universe() shows all. "
                     "terrain(symbol) gives measured excursions for any name.",
    }, indent=2, default=str)
```

- [ ] **Step 2: Verify against live data**

Run:
```bash
.venv/bin/python -c "
import sys,json; sys.path.insert(0,'src')
from agent_env import server
b=json.loads(server.brief())
print('  keys:', list(b))
print('  positions:', len(b['positions']), '| unprotected:', b['unprotected'])
print('  candidates:', list(b['candidates'])[:5])
print('  mandate tradeable:', b['mandate']['tradeable'])
print('  regime:', b['regime'])"
```
Expected: all keys present, 13 positions, a candidate list, `tradeable: True`, and a regime observation.

- [ ] **Step 3: Commit**

```bash
git add src/agent_env/
git commit -m "feat(env): brief() — the whole picture, assembled fresh

A tool rather than a file, so it cannot go stale and slow_loop does not have to
change in this plan. Facts only: the regime line is an observation, not a switch.
Unprotected positions are surfaced at the top level because that is the one thing
the agent must not miss."
```

---

### Task 10: Wire it up and register it with the test runner

**Files:**
- Create: `.mcp.json`
- Modify: `deploy/run_selftests.sh`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces: a project-scoped MCP config Claude Code can launch, and selftest coverage for all four new modules.

- [ ] **Step 1: Create `.mcp.json`**

```json
{
  "mcpServers": {
    "agentic-trader": {
      "command": "/opt/agentic-trader/.venv/bin/python",
      "args": ["/opt/agentic-trader/src/agent_env/server.py"]
    }
  }
}
```

- [ ] **Step 2: Verify Claude Code can see the server**

Run: `claude mcp list 2>&1 | grep -i agentic-trader`
Expected: a line naming `agentic-trader`. If it does not appear, check `.mcp.json` is at the repo root and that the paths are absolute; report what you found rather than guessing.

- [ ] **Step 3: Register the new selftests**

Add to the venv array in `deploy/run_selftests.sh`, matching the existing entries' style: `src/agent_env/state.py`, `src/agent_env/screen.py`, `src/agent_env/terrain.py`, `src/agent_env/decide.py`, `src/agent_env/server.py`.

- [ ] **Step 4: Run the full runner**

Run: `bash deploy/run_selftests.sh`
Expected: `ALL SELFTESTS PASSED`, exit 0, with all five new entries listed.

- [ ] **Step 5: Document the surface in `CLAUDE.md`**

Add to the repo-layout block, in the established style:

```
src/agent_env/                THE AGENT'S ENVIRONMENT (Plan 2 of the inversion) — a
                        FastMCP server exposing what the agent can SEE and DO:
                        brief(), positions(), account(), mandate_status(),
                        candidates(n)/universe(), terrain(symbol),
                        set_levels(sym,stop,target,reason), record_decision(),
                        check_order(). Runs under .venv (3.12) with mcp==1.28.1;
                        needs NO moomoo (quotes reach disk via the monitor).
                        Registered in .mcp.json. NOT yet used by any cron job —
                        the live loops still run the old procedural prompts.
                        server.py = tools; state/screen/terrain/decide = pure
                        helpers, each selftested.
```

- [ ] **Step 6: Final verification and commit**

```bash
bash deploy/run_selftests.sh && python3 src/repo_checks.py
git add .mcp.json deploy/run_selftests.sh CLAUDE.md src/agent_env/
git commit -m "feat(env): register the MCP surface and its selftests

.mcp.json makes the server discoverable to Claude Code; run_selftests.sh gains
all five new modules so a regression in the agent's environment surfaces. No cron
job invokes it yet — the live loops still run the old procedural prompts, and
switching them over is Plan 3."
```

---

## Done when

- `bash deploy/run_selftests.sh` exits 0 with the five new modules registered
- `python3 src/repo_checks.py` passes 6/6
- `claude mcp list` shows `agentic-trader`
- Every tool returns sane output against live data:

```bash
.venv/bin/python -c "
import sys,json; sys.path.insert(0,'src')
from agent_env import server
b=json.loads(server.brief())
print('brief keys      :', list(b))
print('positions       :', len(b['positions']))
print('unprotected     :', b['unprotected'])
print('mandate         :', b['mandate']['tradeable'])
print('candidates      :', list(b['candidates'])[:3])
print('terrain(MU) 5d  :', json.loads(server.terrain('MU'))['horizons']['5'])
print('check MU buy    :', json.loads(server.check_order('MU','buy',5.0))['allowed'])
"
```

**No live trading behaviour changes in this plan.** No cron entry runs the server, `slow_loop`/`fast_loop`/`market_monitor` are untouched, and the agent still executes the old procedural prompts. Plan 3 replaces those prompts with a brief and moves enforcement onto the agent's own levels.
