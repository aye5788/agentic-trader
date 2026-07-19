# Universe Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the fixed 150-name universe an automatic quarterly liquidity refresh with a wide churn band and a continuous weekly stale-seed watch, so it stops silently freezing — auto-applying routine changes and escalating to a human only for seed-drops and anomalies.

**Architecture:** Pure decision logic in `src/universe_maint.py` (read/rank/propose/classify/seed-watch), a data-only moomoo adapter in `src/adapters/moomoo/`, and orchestration in `scripts/universe_refresh.py`. Follows the repo's existing split (pure `src/` module + `scripts/` orchestration, e.g. `momentum.py` ↔ `slow_loop.py`) and its **inline `--selftest`** convention (no pytest).

**Tech Stack:** Python 3.12 (project `.venv`), `moomoo` SDK 10.09.6908 (installed on `/usr/bin/python3`, talks to the running OpenD at `127.0.0.1:11111`), stdlib `csv`/`json`, existing `src/notify.py` (ntfy) and the Resend email path from `scripts/send_newsletter.py`.

## Global Constraints

- **Data-only, offline, never touches the live trading path.** The moomoo adapter exposes NO trading surface and NO live-quote subscription.
- **Dollar-volume = `get_market_snapshot().turnover`** (free, no subscription quota; batches ≤ 400 codes/call). NOT `get_stock_filter` (its TURNOVER field is unsupported).
- **Seeds are never auto-dropped** by the liquidity pass; a stale seed is only ever *surfaced* for human decision.
- **Auto-apply routine; HOLD for human** only on: > `auto_apply_max_changes` changes, any flagged seed, broken/short pond data, or an add that fails sanity.
- **Git is the undo** — every applied change is committed. No new web write-surface; the dashboard stays read-only.
- **Sector tagging is deferred to Piece 2** — new names are written with an empty `sector`; existing sectors carry forward.
- Universe CSV columns (exact order): `ticker,source,sector,exchange,flag,as_of`.
- Run pure selftests with the venv: `python` = `/opt/agentic-trader/.venv/bin/python`. moomoo live probes use `/usr/bin/python3` (that's where the SDK is installed).

---

### Task 1: moomoo data-only adapter

**Files:**
- Create: `src/adapters/moomoo/__init__.py`
- Create: `src/adapters/moomoo/client.py`
- Create: `src/adapters/moomoo/research.py`

**Interfaces:**
- Produces:
  - `client.quote_ctx(host="127.0.0.1", port=11111) -> OpenQuoteContext`
  - `research.snapshot_turnover(tickers: list[str], ctx=None) -> dict[str, float]` (bare ticker → dollar-volume; missing/NaN → 0.0; batches ≤400)
  - `research.screen_top_marketcap(n=400, min_mktcap=2e9, ctx=None) -> list[str]` (bare tickers, market-cap desc, paginated)
  - `research.candidate_pond(incumbents: list[str], pit_pool_path: str, params: dict, ctx=None) -> list[str]` (incumbents ∪ market-cap screen, falling back to the pit_pool CSV if the screen fails/returns empty)
  - `research._bare(code) -> str`, `research._us(ticker) -> str` (code<->ticker helpers)

- [ ] **Step 1: Create the package + client**

Create `src/adapters/moomoo/__init__.py`:
```python
"""moomoo OpenD adapter — DATA ONLY (no trading surface, no live subscription)."""
```

Create `src/adapters/moomoo/client.py`:
```python
"""Connection to the local OpenD gateway. Data-only."""
from moomoo import OpenQuoteContext

HOST = "127.0.0.1"
PORT = 11111


def quote_ctx(host: str = HOST, port: int = PORT) -> OpenQuoteContext:
    """Open a market-data context against the running OpenD. Caller closes it."""
    return OpenQuoteContext(host=host, port=port)
```

- [ ] **Step 2: Write the failing test for the pure helpers**

Create `src/adapters/moomoo/research.py` with only the helpers + a selftest:
```python
"""Data-only moomoo research calls for universe maintenance."""
import csv

from moomoo import RET_OK, Market, SimpleFilter, SortDir, StockField

from .client import quote_ctx


def _us(ticker: str) -> str:
    """Bare ticker -> moomoo US code. 'AAPL' -> 'US.AAPL'; passes through 'US.AAPL'."""
    return ticker if ticker.startswith("US.") else f"US.{ticker}"


def _bare(code: str) -> str:
    """moomoo code -> bare ticker. 'US.AAPL' -> 'AAPL'."""
    return code.split(".", 1)[1] if "." in code else code


def _selftest() -> None:
    assert _us("AAPL") == "US.AAPL"
    assert _us("US.AAPL") == "US.AAPL"
    assert _bare("US.AAPL") == "AAPL"
    assert _bare("AAPL") == "AAPL"
    print("moomoo.research selftest OK: code<->ticker helpers")


if __name__ == "__main__":
    _selftest()
```

Run: `/opt/agentic-trader/.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from adapters.moomoo import research; research._selftest()"`
Expected: FAIL — `ModuleNotFoundError: No module named 'moomoo'` (the project venv lacks the SDK).

- [ ] **Step 3: Make the helper test runnable under the SDK python**

The SDK lives on `/usr/bin/python3`, not the project venv. Verify the helpers there:

Run: `cd /opt/agentic-trader && /usr/bin/python3 -c "import sys; sys.path.insert(0,'src'); from adapters.moomoo import research; research._selftest()"`
Expected: PASS — `moomoo.research selftest OK: code<->ticker helpers`

(Note for later tasks: `universe_maint` pure logic runs under the project `.venv`; only code that imports `moomoo` must run under `/usr/bin/python3`. The cron wrapper in Task 8 uses `/usr/bin/python3` for the whole refresh so the adapter imports resolve.)

- [ ] **Step 4: Implement the live data calls**

Append to `src/adapters/moomoo/research.py` (above `_selftest`):
```python
def snapshot_turnover(tickers, ctx=None) -> dict:
    """{bare_ticker: turnover ($-volume)} via get_market_snapshot, batched <=400.
    turnover is a single-session figure; NaN/missing -> 0.0."""
    own = ctx is None
    q = ctx or quote_ctx()
    out = {}
    try:
        codes = [_us(t) for t in tickers]
        for i in range(0, len(codes), 400):
            ret, df = q.get_market_snapshot(codes[i:i + 400])
            if ret != RET_OK:
                raise RuntimeError(f"get_market_snapshot failed: {df}")
            for _, r in df.iterrows():
                tv = r["turnover"]
                out[_bare(r["code"])] = float(tv) if tv == tv else 0.0  # tv==tv filters NaN
    finally:
        if own:
            q.close()
    return out


def screen_top_marketcap(n: int = 400, min_mktcap: float = 2e9, ctx=None) -> list:
    """Top US names by market cap (a liquidity proxy for the pond). Paginated (200/call)."""
    own = ctx is None
    q = ctx or quote_ctx()
    out = []
    try:
        begin = 0
        while len(out) < n:
            sf = SimpleFilter()
            sf.stock_field = StockField.MARKET_VAL
            sf.filter_min = min_mktcap
            sf.is_no_filter = False
            sf.sort = SortDir.DESCEND
            ret, data = q.get_stock_filter(
                market=Market.US, filter_list=[sf], begin=begin, num=200)
            if ret != RET_OK:
                raise RuntimeError(f"get_stock_filter failed: {data}")
            last_page, _all, lst = data
            out.extend(_bare(s.stock_code) for s in lst)
            if last_page or not lst:
                break
            begin += 200
    finally:
        if own:
            q.close()
    return out[:n]


def candidate_pond(incumbents, pit_pool_path, params, ctx=None) -> list:
    """incumbents ∪ broad reference. Reference = market-cap screen; on any failure or
    empty result, fall back to the pre-built pit_pool CSV. Returns bare tickers, sorted."""
    ref = []
    try:
        ref = screen_top_marketcap(params["screen_top_n"], params["screen_min_mktcap"], ctx=ctx)
    except Exception as e:  # noqa: BLE001 — offline batch; degrade to the static pool
        print(f"  market-cap screen failed ({e}); falling back to pit_pool")
    if not ref:
        with open(pit_pool_path, newline="") as f:
            ref = [row["ticker"] for row in csv.DictReader(f)]
    return sorted(set(incumbents) | set(ref))
```

- [ ] **Step 5: Live probe (network-dependent, kept OUT of the pure selftest suite)**

Run: `cd /opt/agentic-trader && /usr/bin/python3 -c "import sys; sys.path.insert(0,'src'); from adapters.moomoo import research as r; d=r.snapshot_turnover(['AAPL','NVDA']); print(d); assert d['AAPL']>1e9"`
Expected: prints a dict with AAPL turnover in the billions; PASS (no assertion error). If OpenD is down this fails — that is expected and is why it is not in `run_selftests.sh`.

- [ ] **Step 6: Commit**

```bash
cd /opt/agentic-trader
git add src/adapters/moomoo/
git commit -m "feat(moomoo): data-only adapter (snapshot turnover + market-cap pond)"
```

---

### Task 2: Config + universe_maint core (read/write/rank/propose)

**Files:**
- Modify: `config/strategy.toml` (add `[universe_maintenance]`)
- Create: `src/universe_maint.py`
- Create: `scripts/universe_refresh.py` (selftest harness only, for now)

**Interfaces:**
- Consumes: `strategy.load()["universe_maintenance"]` (the param dict)
- Produces:
  - `universe_maint.FIELDS = ["ticker","source","sector","exchange","flag","as_of"]`
  - `universe_maint.read_universe(path) -> list[dict]`
  - `universe_maint.write_universe(path, rows) -> None`
  - `universe_maint.rank_pond(turnovers: dict) -> list[str]`
  - `universe_maint.propose_membership(ranked, turnovers, current_rows, seed_flags, params) -> dict` with keys `keep, drop_fills, add, flagged_seeds, result` (`result` = list of universe rows)

- [ ] **Step 1: Add config block**

In `config/strategy.toml`, append:
```toml
# --- Universe maintenance (Piece 1) — quarterly liquidity refresh + weekly seed-watch ---
[universe_maintenance]
target_size            = 150          # universe size
keep_rank_max          = 180          # incumbent kept while $-vol rank <= this (band)
add_rank_max           = 150          # non-incumbent eligible to add while rank <= this
add_dvol_floor_usd     = 50_000_000   # hard min turnover for an ADD (junk gate)
screen_top_n           = 400          # market-cap screen depth for the pond
screen_min_mktcap      = 2_000_000_000
auto_apply_max_changes = 5            # > this many changes ⇒ HOLD for review
stale_seed_rank_floor  = 100          # seed in bottom third (of 150) counts toward stale
stale_seed_weeks       = 8            # consecutive weeks flagged ⇒ surface for decision
seed_watch_history     = 16           # weeks of rank history to retain per seed
```

Verify it loads:
Run: `cd /opt/agentic-trader && .venv/bin/python -c "import sys; sys.path.insert(0,'src'); import strategy; print(strategy.load()['universe_maintenance']['keep_rank_max'])"`
Expected: `180`

- [ ] **Step 2: Write the failing test (in the refresh harness)**

Create `scripts/universe_refresh.py`:
```python
"""Universe maintenance — quarterly liquidity refresh. Data-only, offline.
See docs/superpowers/specs/2026-07-19-universe-maintenance-design.md."""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import universe_maint as um  # noqa: E402


def _selftest() -> None:
    # rank_pond: descending by turnover, drops non-positive
    r = um.rank_pond({"A": 30.0, "B": 100.0, "C": 0.0, "D": None})
    assert r == ["B", "A"], r

    # propose_membership: seeds protected, fills banded, adds fill open slots
    params = {"target_size": 3, "keep_rank_max": 4, "add_rank_max": 3,
              "add_dvol_floor_usd": 10.0}
    current = [
        {"ticker": "SEED1", "source": "seed", "sector": "X", "exchange": "NASDAQ", "flag": "", "as_of": "2026-01-01"},
        {"ticker": "FILL_OK", "source": "fill_dvol", "sector": "", "exchange": "NYSE", "flag": "", "as_of": "2026-01-01"},
        {"ticker": "FILL_GONE", "source": "fill_dvol", "sector": "", "exchange": "NYSE", "flag": "", "as_of": "2026-01-01"},
    ]
    turn = {"SEED1": 5.0, "FILL_OK": 90.0, "NEW": 80.0, "FILL_GONE": 1.0}
    ranked = ["FILL_OK", "NEW", "SEED1", "FILL_GONE"]  # ranks 1,2,3,4
    p = um.propose_membership(ranked, turn, current, set(), params)
    assert "FILL_GONE" in p["drop_fills"], p        # rank 4 <= keep_max 4? no: strictly beyond band? see rule
    assert "NEW" in p["add"], p                      # rank 2, above floor, open slot
    assert "SEED1" in p["keep"], p                   # seed always kept despite rank 3 / low $vol
    assert len(p["result"]) == 3, p                  # target size respected
    print("universe_maint selftest OK: rank_pond + propose_membership")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return


if __name__ == "__main__":
    main()
```

Note the band rule this test pins down: `FILL_GONE` is rank 4 and `keep_rank_max` is 4, so with a **`>` (strictly beyond)** rule it is KEPT, but the test asserts it is dropped. Resolve by making the test data unambiguous — set `keep_rank_max: 3` in `params` so rank-4 `FILL_GONE` is clearly beyond the band. Update the params line to:
```python
    params = {"target_size": 3, "keep_rank_max": 3, "add_rank_max": 3,
              "add_dvol_floor_usd": 10.0}
```

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/universe_refresh.py --selftest`
Expected: FAIL — `AttributeError: module 'universe_maint' has no attribute 'rank_pond'` (module not created yet).

- [ ] **Step 3: Implement the core**

Create `src/universe_maint.py`:
```python
"""Pure universe-maintenance logic: read/write the CSV, rank by dollar-volume,
propose membership under the seed-protection + band rules. No I/O beyond the CSV
helpers; no moomoo import (that lives in adapters/moomoo)."""
import csv

FIELDS = ["ticker", "source", "sector", "exchange", "flag", "as_of"]


def read_universe(path) -> list:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_universe(path, rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def rank_pond(turnovers: dict) -> list:
    """Tickers by descending dollar-volume; drops None/<=0."""
    valid = {t: v for t, v in turnovers.items() if v and v > 0}
    return sorted(valid, key=lambda t: valid[t], reverse=True)


def propose_membership(ranked, turnovers, current_rows, seed_flags, params) -> dict:
    """Seeds always kept; fills kept while $-vol rank <= keep_rank_max; open slots
    filled from best non-incumbents ranked <= add_rank_max and >= the $-vol floor.
    flagged_seeds = seeds the weekly watch marked stale (surfaced, NOT dropped)."""
    target = params["target_size"]
    keep_max = params["keep_rank_max"]
    add_max = params["add_rank_max"]
    floor = params["add_dvol_floor_usd"]

    rank_of = {t: i + 1 for i, t in enumerate(ranked)}
    big = len(ranked) + 10_000
    by_ticker = {r["ticker"]: r for r in current_rows}
    seeds = [r["ticker"] for r in current_rows if r["source"] == "seed"]
    fills = [r["ticker"] for r in current_rows if r["source"] != "seed"]

    kept = list(seeds)  # 1. seeds always kept
    kept_fills = [t for t in fills if rank_of.get(t, big) <= keep_max]
    dropped_fills = [t for t in fills if rank_of.get(t, big) > keep_max]
    kept += kept_fills

    have = set(kept)  # 3. fill open slots
    open_slots = max(0, target - len(kept))
    adds = []
    for t in ranked:
        if len(adds) >= open_slots:
            break
        if t in have or rank_of[t] > add_max:
            if rank_of[t] > add_max:
                break
            continue
        if turnovers.get(t, 0) < floor:
            continue
        adds.append(t)

    result = [by_ticker[t] for t in kept]
    result += [{"ticker": t, "source": "screen", "sector": "",
                "exchange": "", "flag": "", "as_of": ""} for t in adds]
    return {
        "keep": sorted(kept),
        "drop_fills": sorted(dropped_fills),
        "add": adds,
        "flagged_seeds": sorted(set(seeds) & set(seed_flags)),
        "result": result[:target],
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/universe_refresh.py --selftest`
Expected: PASS — `universe_maint selftest OK: rank_pond + propose_membership`

- [ ] **Step 5: Commit**

```bash
cd /opt/agentic-trader
git add config/strategy.toml src/universe_maint.py scripts/universe_refresh.py
git commit -m "feat(universe): config + core (read/write/rank/propose_membership)"
```

---

### Task 3: classify (auto-apply vs HOLD) + add sanity

**Files:**
- Modify: `src/universe_maint.py` (add `classify`, `_looks_like_common_stock`)
- Modify: `scripts/universe_refresh.py` (extend `_selftest`)

**Interfaces:**
- Produces: `universe_maint.classify(proposal, pond_count, params) -> {"decision": "AUTO_APPLY"|"HOLD", "reasons": [str]}`

- [ ] **Step 1: Write failing test (extend `_selftest`)**

In `scripts/universe_refresh.py::_selftest`, before the final `print`, add:
```python
    cp = {"target_size": 150, "auto_apply_max_changes": 5}
    # routine → AUTO_APPLY
    small = {"add": ["NEW"], "drop_fills": ["OLD"], "flagged_seeds": []}
    assert um.classify(small, 400, cp)["decision"] == "AUTO_APPLY"
    # too many changes → HOLD
    big = {"add": ["A", "B", "C", "D"], "drop_fills": ["E", "F", "G"], "flagged_seeds": []}
    assert um.classify(big, 400, cp)["decision"] == "HOLD"
    # flagged seed → HOLD
    seed = {"add": [], "drop_fills": [], "flagged_seeds": ["MU"]}
    assert um.classify(seed, 400, cp)["decision"] == "HOLD"
    # short pond (broken data) → HOLD
    assert um.classify(small, 100, cp)["decision"] == "HOLD"
    # non-common-stock add (leveraged/odd ticker) → HOLD
    bad = {"add": ["SOXL"], "drop_fills": [], "flagged_seeds": []}
    assert um.classify(bad, 400, cp)["decision"] == "HOLD"
    print("universe_maint selftest OK: classify")
```
Also change the previous final `print(...)` line to `print("universe_maint selftest OK: rank_pond + propose_membership")` stays as-is (two prints total is fine).

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/universe_refresh.py --selftest`
Expected: FAIL — `AttributeError: module 'universe_maint' has no attribute 'classify'`

- [ ] **Step 2: Implement**

Append to `src/universe_maint.py`:
```python
import re

_LEVERAGED = {"SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXU", "TNA", "TZA",
              "UVXY", "SVXY", "UPRO", "SDOW", "UDOW", "LABU", "LABD"}


def _looks_like_common_stock(ticker: str) -> bool:
    """v1 sanity: 1-5 uppercase letters, not a known leveraged/inverse ETF."""
    return bool(re.fullmatch(r"[A-Z]{1,5}", ticker)) and ticker not in _LEVERAGED


def classify(proposal, pond_count, params) -> dict:
    """Decide auto-apply vs. hold-for-human. HOLD on any anomaly/judgment case."""
    reasons = []
    changes = len(proposal["add"]) + len(proposal["drop_fills"])
    if changes > params["auto_apply_max_changes"]:
        reasons.append(f"{changes} changes > {params['auto_apply_max_changes']} (possible data glitch)")
    if proposal["flagged_seeds"]:
        reasons.append("stale seed(s) up for decision: " + ", ".join(proposal["flagged_seeds"]))
    if pond_count < params["target_size"]:
        reasons.append(f"pond only {pond_count} names (< target {params['target_size']}) — data may be broken")
    bad = [t for t in proposal["add"] if not _looks_like_common_stock(t)]
    if bad:
        reasons.append("add(s) failed sanity: " + ", ".join(bad))
    return {"decision": "HOLD" if reasons else "AUTO_APPLY", "reasons": reasons}
```

- [ ] **Step 3: Run to verify pass**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/universe_refresh.py --selftest`
Expected: PASS — both selftest lines print.

- [ ] **Step 4: Commit**

```bash
cd /opt/agentic-trader
git add src/universe_maint.py scripts/universe_refresh.py
git commit -m "feat(universe): classify (auto-apply vs hold) + add sanity"
```

---

### Task 4: Weekly stale-seed watch + slow-loop hook

**Files:**
- Modify: `src/universe_maint.py` (add `update_seed_watch`, `flag_stale_seeds`)
- Modify: `scripts/slow_loop.py` (append seed ranks after ranking)
- Modify: `scripts/universe_refresh.py` (extend `_selftest`)

**Interfaces:**
- Produces:
  - `universe_maint.update_seed_watch(watch: dict, seed_ranks: dict, max_history: int) -> dict`
  - `universe_maint.flag_stale_seeds(watch: dict, params) -> list[str]`
- Consumes (in slow_loop): the momentum ranking already computed there (per-ticker rank), plus `read_universe` to know which tickers are seeds.

- [ ] **Step 1: Write failing test (extend `_selftest`)**

Add before the final print in `_selftest`:
```python
    w = {}
    w = um.update_seed_watch(w, {"MU": 120, "AAPL": 5}, max_history=3)
    w = um.update_seed_watch(w, {"MU": 130, "AAPL": 4}, max_history=3)
    assert w["MU"] == [120, 130] and w["AAPL"] == [5, 4], w
    sp = {"stale_seed_rank_floor": 100, "stale_seed_weeks": 2}
    assert um.flag_stale_seeds(w, sp) == ["MU"], um.flag_stale_seeds(w, sp)  # MU bottom-third 2x; AAPL not
    # history cap
    w2 = um.update_seed_watch({"X": [1, 2, 3]}, {"X": 4}, max_history=3)
    assert w2["X"] == [2, 3, 4], w2
    print("universe_maint selftest OK: seed-watch")
```

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/universe_refresh.py --selftest`
Expected: FAIL — `AttributeError: ... 'update_seed_watch'`

- [ ] **Step 2: Implement the watch functions**

Append to `src/universe_maint.py`:
```python
def update_seed_watch(watch: dict, seed_ranks: dict, max_history: int) -> dict:
    """Append this week's momentum rank per seed; retain last max_history weeks."""
    for t, rank in seed_ranks.items():
        hist = watch.setdefault(t, [])
        hist.append(rank)
        watch[t] = hist[-max_history:]
    return watch


def flag_stale_seeds(watch: dict, params) -> list:
    """A seed is stale if its momentum rank has been worse than stale_seed_rank_floor
    for stale_seed_weeks consecutive weeks."""
    floor = params["stale_seed_rank_floor"]
    weeks = params["stale_seed_weeks"]
    flagged = [t for t, hist in watch.items()
               if len(hist) >= weeks and all(r > floor for r in hist[-weeks:])]
    return sorted(flagged)
```

- [ ] **Step 3: Run to verify pass**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/universe_refresh.py --selftest`
Expected: PASS — `universe_maint selftest OK: seed-watch`

- [ ] **Step 4: Hook the accrual into the slow loop**

First read where the slow loop finishes ranking:
Run: `cd /opt/agentic-trader && grep -n "def \|rank\|write_product\|compute\|select(" scripts/slow_loop.py | head -40`

Add this block at the **end of `main()`** in `scripts/slow_loop.py` (after the monitor-file cleanup block near line ~220 — the last thing the run does). It uses the real locals confirmed by the grep: `book_scored` (the single-name momentum DataFrame, indexed by ticker with a `rank` column, from `mom.compute` at line 150), `names` (single-name tickers present in the price panel), `cfg` (from `strat.load()`), and `REPO`:
```python
    # --- weekly stale-seed watch accrual (universe maintenance, Piece 1) ---
    try:
        import json as _json
        import universe_maint as _um
        cfg_um = cfg["universe_maintenance"]
        uni_rows = _um.read_universe(str(REPO / "config" / "universe.csv"))
        seeds = {r["ticker"] for r in uni_rows if r["source"] == "seed"}

        def _rk(sym):
            r = book_scored.loc[sym, "rank"]
            return int(r) if r == r else 9999  # NaN (ineligible seed) => worst-rank ⇒ counts as stale

        seed_ranks = {s: _rk(s) for s in names if s in seeds and s in book_scored.index}
        watch_path = REPO / "research_store" / "universe" / "seed_watch.json"
        watch_path.parent.mkdir(parents=True, exist_ok=True)
        watch = _json.loads(watch_path.read_text()) if watch_path.exists() else {}
        watch = _um.update_seed_watch(watch, seed_ranks, cfg_um["seed_watch_history"])
        watch_path.write_text(_json.dumps(watch, indent=2))
        print(f"seed-watch: recorded ranks for {len(seed_ranks)} seeds -> {watch_path}")
    except Exception as e:  # never let the watch break the slow loop
        print(f"seed-watch accrual skipped: {e}")
```
Note the deliberate `9999` for a seed with a NaN rank (ineligible = negative 12-month return): that's exactly the seed we WANT flagged, so it must count as bottom-rank, not be skipped.

- [ ] **Step 5: Verify the slow loop still runs (dry, no live orders — it never trades)**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/slow_loop.py 2>&1 | tail -20`
Expected: completes as before AND prints `seed-watch: recorded ranks for N seeds`, and `research_store/universe/seed_watch.json` now exists.
Run: `cat research_store/universe/seed_watch.json | head`
Expected: a JSON object of `{ticker: [rank]}` for the seed names.

- [ ] **Step 6: Commit**

```bash
cd /opt/agentic-trader
git add src/universe_maint.py scripts/universe_refresh.py scripts/slow_loop.py
git commit -m "feat(universe): weekly stale-seed watch + slow-loop accrual hook"
```

---

### Task 5: Orchestration — the refresh run (dry-run + artifacts + notify + auto/hold)

**Files:**
- Modify: `scripts/universe_refresh.py` (add `run()` + `--dry-run`/`--run`)
- Modify: `deploy/run_selftests.sh` (add the refresh selftest)

**Interfaces:**
- Consumes: Task 1 adapter (`snapshot_turnover`, `candidate_pond`), Task 2-4 `universe_maint`, `strategy.load()`, `src/notify.py::push`, `flag_stale_seeds`, `research_store/universe/seed_watch.json`.
- Produces: `research_store/universe/proposals/<YYYY-MM-DD>.json` and `.md`; on AUTO_APPLY also writes `config/universe.csv` (via Task 6's apply, called inline); a ntfy push either way.

- [ ] **Step 1: Implement `run()`**

Add to `scripts/universe_refresh.py` (imports at top: `import json`, `from datetime import datetime, timezone`; and `from adapters.moomoo import research as mm`; and `import strategy`, `from notify import push`). Add:
```python
UNI = REPO / "config" / "universe.csv"
POOL = REPO / "config" / "pit_pool.csv"
PROP_DIR = REPO / "research_store" / "universe" / "proposals"
WATCH = REPO / "research_store" / "universe" / "seed_watch.json"


def _render_md(proposal, decision, asof) -> str:
    L = [f"# Universe proposal {asof}", "",
         f"**Decision:** {decision['decision']}"]
    if decision["reasons"]:
        L += ["", "**Held because:**"] + [f"- {r}" for r in decision["reasons"]]
    L += ["", f"**Adds ({len(proposal['add'])}):** " + (", ".join(proposal["add"]) or "none"),
          f"**Drops — fills ({len(proposal['drop_fills'])}):** " + (", ".join(proposal["drop_fills"]) or "none"),
          f"**Flagged seeds (your decision):** " + (", ".join(proposal["flagged_seeds"]) or "none")]
    return "\n".join(L)


def run(asof: str, dry: bool) -> dict:
    cfg = strategy.load()
    params = cfg["universe_maintenance"]
    current = um.read_universe(str(UNI))
    incumbents = [r["ticker"] for r in current]
    watch = json.loads(WATCH.read_text()) if WATCH.exists() else {}
    seed_flags = um.flag_stale_seeds(watch, params)

    pond = mm.candidate_pond(incumbents, str(POOL), params)
    turnovers = mm.snapshot_turnover(pond)
    ranked = um.rank_pond(turnovers)
    proposal = um.propose_membership(ranked, turnovers, current, seed_flags, params)
    decision = um.classify(proposal, len(ranked), params)

    PROP_DIR.mkdir(parents=True, exist_ok=True)
    slim = {k: proposal[k] for k in ("keep", "drop_fills", "add", "flagged_seeds")}
    (PROP_DIR / f"{asof}.json").write_text(json.dumps(
        {"asof": asof, "decision": decision, **slim}, indent=2))
    md = _render_md(proposal, decision, asof)
    (PROP_DIR / f"{asof}.md").write_text(md)
    print(md)

    if dry:
        print("\n[dry-run] no changes written, no notification sent")
        return {"proposal": proposal, "decision": decision}

    if decision["decision"] == "AUTO_APPLY":
        apply_proposal(proposal, asof, cfg)  # Task 6
        push("Universe auto-refreshed",
             f"−{','.join(proposal['drop_fills']) or 'none'}  +{','.join(proposal['add']) or 'none'} (committed)",
             tags="recycle")
    else:
        push("Universe proposal needs review",
             f"HOLD: {'; '.join(decision['reasons'])}\nApprove in a Claude session.",
             tags="warning")
    return {"proposal": proposal, "decision": decision}
```

Add args in `main()`:
```python
    ap.add_argument("--run", action="store_true", help="run the refresh (auto-apply or hold)")
    ap.add_argument("--dry-run", action="store_true", help="compute + write proposal, change nothing")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD stamp (default: today UTC)")
```
and in the body after the selftest branch:
```python
    if args.run or args.dry_run:
        asof = args.asof or datetime.now(timezone.utc).date().isoformat()
        run(asof, dry=args.dry_run)
        return
```

- [ ] **Step 2: Add the refresh selftest to the suite**

In `deploy/run_selftests.sh`, add `"scripts/universe_refresh.py"` to the `SELFTESTS` array.

Run: `cd /opt/agentic-trader && deploy/run_selftests.sh 2>&1 | tail -8`
Expected: all selftests PASS, including `universe_refresh.py --selftest`.

- [ ] **Step 3: Live dry-run (network-dependent — uses OpenD)**

Run: `cd /opt/agentic-trader && /usr/bin/python3 scripts/universe_refresh.py --dry-run 2>&1 | tail -30`
Expected: prints a Markdown proposal (adds/drops/flagged), writes `research_store/universe/proposals/<today>.{json,md}`, changes nothing, sends no push. (Requires `apply_proposal` to at least be importable — if Task 6 isn't done yet, run this step after Task 6.)

- [ ] **Step 4: Commit**

```bash
cd /opt/agentic-trader
git add scripts/universe_refresh.py deploy/run_selftests.sh
git commit -m "feat(universe): refresh orchestration (dry-run, proposal artifacts, notify)"
```

---

### Task 6: Apply path (write universe.csv + git commit)

**Files:**
- Modify: `scripts/universe_refresh.py` (add `apply_proposal` + `--apply <asof>`)

**Interfaces:**
- Produces: `apply_proposal(proposal: dict, asof: str, cfg: dict) -> None` — stamps `as_of`, writes `config/universe.csv` via `um.write_universe`, `git add`+`commit`.
- Consumes: a held proposal JSON at `research_store/universe/proposals/<asof>.json` (for `--apply`).

- [ ] **Step 1: Write failing test (the CSV writer round-trips + stamps as_of)**

Add to `_selftest` before the final print:
```python
    import tempfile, os
    rows = [{"ticker": "AAA", "source": "seed", "sector": "S", "exchange": "NYSE", "flag": "", "as_of": "old"},
            {"ticker": "BBB", "source": "screen", "sector": "", "exchange": "", "flag": "", "as_of": ""}]
    fd, tmp = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    for r in rows:
        r["as_of"] = "2026-07-19"
    um.write_universe(tmp, rows)
    back = um.read_universe(tmp)
    assert [r["ticker"] for r in back] == ["AAA", "BBB"], back
    assert all(r["as_of"] == "2026-07-19" for r in back), back
    assert list(back[0].keys()) == um.FIELDS, back[0]
    os.remove(tmp)
    print("universe_maint selftest OK: csv round-trip + as_of stamp")
```

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/universe_refresh.py --selftest`
Expected: PASS (this exercises Task 2's `write_universe`/`read_universe` — confirms the writer contract the apply relies on).

- [ ] **Step 2: Implement `apply_proposal` + `--apply`**

Add to `scripts/universe_refresh.py` (top: `import subprocess`):
```python
def apply_proposal(proposal, asof, cfg) -> None:
    """Stamp as_of, write config/universe.csv, commit. Git = the undo."""
    rows = [dict(r) for r in proposal["result"]]
    for r in rows:
        r["as_of"] = asof
    um.write_universe(str(UNI), rows)
    subprocess.run(["git", "add", str(UNI)], cwd=str(REPO), check=True)
    msg = (f"chore(universe): refresh {asof} "
           f"(+{len(proposal['add'])} −{len(proposal['drop_fills'])})")
    subprocess.run(["git", "commit", "-m", msg], cwd=str(REPO), check=True)
    print(f"applied: universe.csv written + committed ({msg})")


def apply_from_file(asof, cfg) -> None:
    """Human-gated apply of a previously HELD proposal (via a Claude session)."""
    data = json.loads((PROP_DIR / f"{asof}.json").read_text())
    current = um.read_universe(str(UNI))
    by = {r["ticker"]: r for r in current}
    # rebuild result rows: kept incumbents (existing rows) + adds (fresh rows)
    result = [by[t] for t in data["keep"] if t in by]
    result += [{"ticker": t, "source": "screen", "sector": "", "exchange": "",
                "flag": "", "as_of": ""} for t in data["add"]]
    apply_proposal({"result": result, "add": data["add"], "drop_fills": data["drop_fills"]}, asof, cfg)
```
Add to `main()`:
```python
    ap.add_argument("--apply", metavar="ASOF", default=None,
                    help="apply a previously held proposal by its YYYY-MM-DD id")
```
and:
```python
    if args.apply:
        apply_from_file(args.apply, strategy.load())
        return
```

- [ ] **Step 3: Run the pure selftest to confirm no regressions**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/universe_refresh.py --selftest`
Expected: all selftest lines PASS.

- [ ] **Step 4: End-to-end dry-run then a guarded live apply on a throwaway branch**

Run (safe — a branch, reverted): 
```bash
cd /opt/agentic-trader && git switch -c tmp-universe-test && \
  /usr/bin/python3 scripts/universe_refresh.py --dry-run --asof 2026-07-19 && \
  echo "dry-run wrote proposal:" && ls research_store/universe/proposals/
```
Expected: a proposal `.json`/`.md` exist; `config/universe.csv` unchanged (dry-run). Then clean up:
```bash
cd /opt/agentic-trader && git switch main && git branch -D tmp-universe-test
```

- [ ] **Step 5: Commit**

```bash
cd /opt/agentic-trader
git add scripts/universe_refresh.py
git commit -m "feat(universe): apply path (write universe.csv + git commit) + --apply"
```

---

### Task 7: Review surfaces — proposal email (Resend) + dashboard panel

**Files:**
- Modify: `scripts/universe_refresh.py` (email a held proposal)
- Modify: `dashboard/app.py` (pending-proposal read)
- Modify: `dashboard/dashboard.html` (panel)

**Interfaces:**
- Consumes: the Resend send helper in `scripts/send_newsletter.py` and `research_store/universe/proposals/<asof>.md`.
- Produces: an email on HOLD; a read-only dashboard panel showing the latest proposal.

- [ ] **Step 1: Confirm the reusable Resend sender (already located)**

The sender is `scripts/send_newsletter.py::_send_resend(api_key, sender, to, subject, html)`,
reading env vars `RESEND_API_KEY`, `NEWSLETTER_TO`, `NEWSLETTER_FROM`. Confirm it's unchanged:
Run: `cd /opt/agentic-trader && grep -n "_send_resend\|RESEND_API_KEY\|NEWSLETTER_TO\|NEWSLETTER_FROM" scripts/send_newsletter.py`
Expected: the `_send_resend(api_key, sender, to, subject, html)` def and those three env vars.

- [ ] **Step 2: Email the held proposal**

In `scripts/universe_refresh.py`, in `run()`, inside the `else` (HOLD) branch, after the `push(...)`, add:
```python
        try:
            import os
            import importlib.util
            to = os.environ.get("NEWSLETTER_TO")
            sender = os.environ.get("NEWSLETTER_FROM")
            api_key = os.environ.get("RESEND_API_KEY")
            if to and sender and api_key:
                spec = importlib.util.spec_from_file_location(
                    "send_newsletter", str(REPO / "scripts" / "send_newsletter.py"))
                sn = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(sn)
                html = "<pre>" + md.replace("<", "&lt;") + "</pre>"
                sn._send_resend(api_key, sender, to, f"Universe proposal {asof} — needs review", html)
                print("held proposal emailed via resend")
            else:
                print("proposal email skipped (RESEND_API_KEY/NEWSLETTER_TO/FROM not set)")
        except Exception as e:  # email is best-effort; the push + dashboard still cover it
            print(f"proposal email skipped: {e}")
```

- [ ] **Step 3: Add the dashboard panel (read-only)**

Run: `cd /opt/agentic-trader && grep -n "route\|render\|def index\|research_store" dashboard/app.py | head -20`
In `dashboard/app.py`, in the main view function, load the latest proposal:
```python
    import json as _json
    from pathlib import Path as _P
    _pd = _P(app.root_path).parent / "research_store" / "universe" / "proposals"
    pending = None
    if _pd.exists():
        files = sorted(_pd.glob("*.json"))
        if files:
            d = _json.loads(files[-1].read_text())
            if d.get("decision", {}).get("decision") == "HOLD":
                pending = d
```
Pass `pending=pending` to `render_template(...)`. In `dashboard/dashboard.html`, add near the top of the body:
```html
{% if pending %}
<div class="panel warn">
  <h3>⚠ Universe proposal pending review ({{ pending.asof }})</h3>
  <p>Held: {{ pending.decision.reasons|join('; ') }}</p>
  <p>Adds: {{ pending.add|join(', ') or 'none' }} · Drops: {{ pending.drop_fills|join(', ') or 'none' }}
     · Flagged seeds: {{ pending.flagged_seeds|join(', ') or 'none' }}</p>
  <p><em>Approve in a Claude session: "apply the pending universe proposal".</em></p>
</div>
{% endif %}
```

- [ ] **Step 4: Verify the dashboard still renders**

Run: `cd /opt/agentic-trader && .venv/bin/python -c "import sys; sys.path.insert(0,'dashboard'); import app; c=app.app.test_client(); import base64,os; h={'Authorization':'Basic '+base64.b64encode((os.getenv('DASH_USER','')+':'+os.getenv('DASH_PASS','')).encode()).decode()}; r=c.get('/',headers=h); print('status',r.status_code)"`
Expected: `status 200` (or `401` if creds not in env — then just confirm no template error by checking the app imports cleanly: `.venv/bin/python -c "import sys; sys.path.insert(0,'dashboard'); import app; print('ok')"` → `ok`).

- [ ] **Step 5: Commit**

```bash
cd /opt/agentic-trader
git add scripts/universe_refresh.py dashboard/app.py dashboard/dashboard.html
git commit -m "feat(universe): review surfaces — held-proposal email + dashboard panel"
```

---

### Task 8: Scheduling (quarterly cron)

**Files:**
- Create: `deploy/run_universe_refresh.sh`
- Modify: `deploy/crontab.template`

**Interfaces:**
- Consumes: `scripts/universe_refresh.py --run`, run under `/usr/bin/python3` (the moomoo SDK's interpreter).

- [ ] **Step 1: Create the wrapper**

Create `deploy/run_universe_refresh.sh`:
```bash
#!/usr/bin/env bash
# Quarterly universe liquidity refresh (Piece 1). Runs under /usr/bin/python3
# because the moomoo SDK lives there. Auto-applies routine changes; HOLDs + alerts
# on seed-drops/anomalies. Never touches the live trading path.
set -euo pipefail
cd /opt/agentic-trader
exec /usr/bin/python3 scripts/universe_refresh.py --run
```
Then: `chmod +x deploy/run_universe_refresh.sh`

- [ ] **Step 2: Add the cron entry**

In `deploy/crontab.template`, in the agentic-trader section, add:
```cron
# --- QUARTERLY UNIVERSE REFRESH (Piece 1) — first Sunday of Jan/Apr/Jul/Oct, 19:00 ET ---
# Auto-applies routine liquidity rotations; HOLDs + phone-alerts on seed-drops/anomalies.
0 19 1-7 1,4,7,10 0  /opt/agentic-trader/deploy/run_universe_refresh.sh >> /opt/agentic-trader/logs/universe.log 2>&1
```
(The `1-7 … 0` day-of-month + day-of-week combination fires only on the *first Sunday* of those months.)

- [ ] **Step 3: Verify the wrapper runs (dry, not installing cron)**

Run: `cd /opt/agentic-trader && bash -n deploy/run_universe_refresh.sh && echo "syntax ok"`
Expected: `syntax ok`
Run (live, offline — writes a proposal, may auto-apply/commit): only run intentionally; otherwise use the `--dry-run` form from Task 5 Step 3 to validate without side effects.

- [ ] **Step 4: Commit**

```bash
cd /opt/agentic-trader
git add deploy/run_universe_refresh.sh deploy/crontab.template
git commit -m "feat(universe): quarterly refresh cron + wrapper (first Sunday Jan/Apr/Jul/Oct)"
```

- [ ] **Step 5: Install the cron (operator step — do when ready to arm)**

This is a live-scheduling action; do it deliberately, not as part of the build:
```bash
crontab /opt/agentic-trader/deploy/crontab.template && crontab -l | grep universe
```
Expected: the universe-refresh line appears in the active crontab.

---

## Notes for the implementer

- **Two interpreters:** pure `universe_maint` + selftests run under the project `.venv`; anything importing `moomoo` (the adapter, the live `--run`/`--dry-run`) runs under `/usr/bin/python3`. The cron wrapper uses `/usr/bin/python3` so the whole refresh resolves the SDK.
- **Shared OpenD:** the vol-desk also uses OpenD; the refresh only issues snapshots + one screener call (a brief quarterly/weekly batch), so there's no subscription-slot contention. Do not open long-lived subscriptions.
- **The slow-loop hook (Task 4) must never break the loop** — it's wrapped in try/except and only appends to a JSON file.
- **First real run** may exceed the 5-change auto-apply threshold (the universe is stale) → it will HOLD for your review. That's intended.
