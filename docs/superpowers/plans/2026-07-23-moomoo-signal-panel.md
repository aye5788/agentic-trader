# moomoo Signal Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Weekly, automatic, per-held-name snapshot of four moomoo signals, distilled to compact scalars and appended to the ledger as one `signal_panel` journal event — forward-collection for later meta-labeling.

**Architecture:** A pure distillation module (`src/signal_panel.py`, TDD via `--selftest`) turns raw moomoo frames into scalars. Four thin pull functions in the moomoo adapter fetch the data. A standalone orchestrator (`scripts/collect_signals.py`) — run under **system `/usr/bin/python3`** after the Sunday rebalance — reads the book, pulls, distills, and appends the event; non-fatal, with an OpenD-gap phone alert.

**Tech Stack:** Python; `moomoo` SDK (system python3.10 only) via OpenD `127.0.0.1:11111`; pandas (present under 3.10); the repo's inline `--selftest` convention (no pytest). Reuses `research_store.read_current`, `research_store.store.append_journal`, `notify.push` (all verified to import under 3.10).

**Spec:** [`docs/superpowers/specs/2026-07-23-moomoo-signal-panel-design.md`](../specs/2026-07-23-moomoo-signal-panel-design.md)

## Global Constraints

- **Never trades / never gates.** Reads moomoo data, appends to the journal. No order path, no `unlock_trade`.
- **Non-fatal by contract.** OpenD down / a name failing / partial pull → log `null` + a `gaps` entry, exit 0. Never crash the cycle.
- **Runtime seam.** The collector runs under **`/usr/bin/python3` (3.10)**, NOT `.venv`. moomoo cannot import in `.venv`.
- **Book-scoped.** Only names with `target_weight > 0` in the current product.
- **Locked fields (exact names):** `capflow_bignet_20d`, `short_pct`, `days_to_cover`, `short_pct_chg`, `pc_vol_ratio`, `pc_oi_ratio`, `iv_rank`, `pct_52w_high`, `volume_ratio`.
- **`capflow_bignet_20d` = Σ(last 20 daily `super_in_flow + big_in_flow`) ÷ `total_market_val`.**
- **Test convention:** inline `def _selftest()` guarded by `if __name__ == "__main__": if "--selftest" in sys.argv: _selftest()`, ending `print("selftest OK: ...")`. Mirror `src/ledger.py`. `from __future__ import annotations`.
- **Event schema:** one `signal_panel` event/week: `{event, as_of, at, source:"moomoo", names:{SYM:{decision_id, <9 fields>}}, opend_ok, gaps:[]}`.

---

### Task 1: Pure distillation module (`src/signal_panel.py`)

Turns raw moomoo records into the locked scalars. Pure — no I/O, no OpenD — so it's fully `--selftest`-able. Distillers take **lists of dicts** (records) / a single dict, not DataFrames, so tests use plain fixtures.

**Files:**
- Create: `src/signal_panel.py`

**Interfaces:**
- Produces:
  - `distill_capflow(rows: list[dict], market_val: float) -> float | None` — rows have `super_in_flow`, `big_in_flow`, `capital_flow_item_time`.
  - `distill_short(rows: list[dict]) -> dict` → `{short_pct, days_to_cover, short_pct_chg}` (rows have `short_percent`, `days_to_cover`, `timestamp_str`).
  - `distill_options(row: dict) -> dict` → `{pc_vol_ratio, pc_oi_ratio, iv_rank}`.
  - `distill_snapshot(row: dict) -> dict` → `{pct_52w_high, volume_ratio}`.

- [ ] **Step 1: Write the failing selftest**

Create `src/signal_panel.py`:

```python
"""Pure distillation of raw moomoo records into the locked signal-panel scalars.

No I/O, no OpenD, no clock — dataframes are converted to list-of-dicts by the
caller (scripts/collect_signals.py) and passed in. Each distiller is null-safe:
missing/degenerate inputs return None rather than raising, because the collector
is non-fatal by contract.

Locked fields (see docs/superpowers/specs/2026-07-23-moomoo-signal-panel-design.md):
  capflow_bignet_20d, short_pct, days_to_cover, short_pct_chg,
  pc_vol_ratio, pc_oi_ratio, iv_rank, pct_52w_high, volume_ratio
"""
from __future__ import annotations


def _num(x):
    try:
        v = float(x)
        return v if v == v else None  # NaN -> None
    except (TypeError, ValueError):
        return None


def distill_capflow(rows, market_val):
    """Σ over the last 20 daily bars of (super+big net inflow) ÷ market cap.
    rows: daily capital-flow records (any order). None if no rows or no mktcap."""
    mv = _num(market_val)
    if not rows or not mv:
        return None
    ordered = sorted(rows, key=lambda r: r.get("capital_flow_item_time", ""))
    last20 = ordered[-20:]
    total = 0.0
    for r in last20:
        s = _num(r.get("super_in_flow")) or 0.0
        b = _num(r.get("big_in_flow")) or 0.0
        total += s + b
    return round(total / mv, 6)


def distill_short(rows):
    """Latest short_percent + days_to_cover, and the change vs the prior reading.
    rows sorted newest-first by timestamp; short_pct_chg is None with <2 rows."""
    out = {"short_pct": None, "days_to_cover": None, "short_pct_chg": None}
    if not rows:
        return out
    ordered = sorted(rows, key=lambda r: r.get("timestamp_str", ""), reverse=True)
    latest = ordered[0]
    out["short_pct"] = _num(latest.get("short_percent"))
    out["days_to_cover"] = _num(latest.get("days_to_cover"))
    if len(ordered) >= 2:
        prev = _num(ordered[1].get("short_percent"))
        if out["short_pct"] is not None and prev is not None:
            out["short_pct_chg"] = round(out["short_pct"] - prev, 4)
    return out


def _ratio(num, den):
    n, d = _num(num), _num(den)
    if n is None or not d:      # den None or 0 -> None
        return None
    return round(n / d, 4)


def distill_options(row):
    """put/call volume + OI ratios and iv_rank from an option-overview row."""
    row = row or {}
    return {
        "pc_vol_ratio": _ratio(row.get("put_volume"), row.get("call_volume")),
        "pc_oi_ratio": _ratio(row.get("put_open_interest"), row.get("call_open_interest")),
        "iv_rank": _num(row.get("iv_rank")),
    }


def distill_snapshot(row):
    """52-week-high proximity + abnormal-volume ratio from a market snapshot row."""
    row = row or {}
    return {
        "pct_52w_high": _ratio(row.get("last_price"), row.get("highest52weeks_price")),
        "volume_ratio": _num(row.get("volume_ratio")),
    }


def _selftest() -> None:
    # capflow: (10+20)+(5+5)+(-3+8) = 45 ; /1000 = 0.045
    cf = [
        {"capital_flow_item_time": "2026-07-01", "super_in_flow": 10, "big_in_flow": 20},
        {"capital_flow_item_time": "2026-07-02", "super_in_flow": 5, "big_in_flow": 5},
        {"capital_flow_item_time": "2026-07-03", "super_in_flow": -3, "big_in_flow": 8},
    ]
    assert distill_capflow(cf, 1000.0) == 0.045, distill_capflow(cf, 1000.0)
    assert distill_capflow(cf, 0) is None          # no mktcap -> None
    assert distill_capflow([], 1000.0) is None      # no rows -> None

    # short: newest 0.7/1.4 ; chg = 0.7 - 0.9 = -0.2
    si = [
        {"timestamp_str": "2026-06-30", "short_percent": 0.7, "days_to_cover": 1.4},
        {"timestamp_str": "2026-06-15", "short_percent": 0.9, "days_to_cover": 1.6},
    ]
    s = distill_short(si)
    assert s == {"short_pct": 0.7, "days_to_cover": 1.4, "short_pct_chg": -0.2}, s
    one = distill_short([si[0]])
    assert one["short_pct"] == 0.7 and one["short_pct_chg"] is None, one
    assert distill_short([]) == {"short_pct": None, "days_to_cover": None, "short_pct_chg": None}

    # options: 320/400=0.8 ; 2000/2700=0.7407 ; iv_rank passthrough ; 0-guard
    o = distill_options({"call_volume": 400, "put_volume": 320,
                         "call_open_interest": 2700, "put_open_interest": 2000,
                         "iv_rank": 77.7})
    assert o == {"pc_vol_ratio": 0.8, "pc_oi_ratio": 0.7407, "iv_rank": 77.7}, o
    assert distill_options({"call_volume": 0, "put_volume": 5})["pc_vol_ratio"] is None

    # snapshot: 320/335 = 0.9552
    sn = distill_snapshot({"last_price": 320, "highest52weeks_price": 335, "volume_ratio": 0.54})
    assert sn == {"pct_52w_high": 0.9552, "volume_ratio": 0.54}, sn
    assert distill_snapshot({"last_price": 320, "highest52weeks_price": 0})["pct_52w_high"] is None

    print("selftest OK: distill_capflow/short/options/snapshot")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 src/signal_panel.py --selftest`
Expected: FAIL — `NameError: name 'distill_capflow' is not defined` (Step 1 wrote the selftest above the implementations, so it errors until Step 3 adds them).

Wait — Step 1 already contains the full implementations. So the selftest passes immediately. That is expected: this module is pure transcription with verified vectors. Treat Step 2 as: run and confirm it PASSES with `selftest OK: ...`. (If it fails, you mistranscribed — diff against the plan, do not change the asserted numbers; they were computed by hand: 45/1000=0.045, 0.7−0.9=−0.2, 2000/2700=0.7407, 320/335=0.9552.)

- [ ] **Step 3: (already implemented in Step 1) — run to verify pass**

Run: `python3 src/signal_panel.py --selftest`
Expected: `selftest OK: distill_capflow/short/options/snapshot`

- [ ] **Step 4: Commit**

```bash
git add src/signal_panel.py
git commit -m "feat(signals): pure distillation of moomoo records -> panel scalars

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: moomoo pull functions (`src/adapters/moomoo/research.py`)

Four thin I/O wrappers that fetch the raw records the distillers need. These need OpenD, so they're verified by a **live dry-run** on the box (OpenD up), not a pure unit test. Defensive tuple-normalization (some moomoo calls return >2-tuples — `docs/DATA_SOURCES.md` §5d).

**Files:**
- Modify: `src/adapters/moomoo/research.py`

**Interfaces:**
- Consumes: `quote_ctx` and `_us` / `_bare` (already in this module); `moomoo.RET_OK`, `moomoo.PeriodType`.
- Produces:
  - `capital_flow_daily(ctx, ticker) -> list[dict]` — daily records (`super_in_flow`, `big_in_flow`, `capital_flow_item_time`).
  - `short_interest(ctx, ticker) -> list[dict]` — records (`short_percent`, `days_to_cover`, `timestamp_str`).
  - `option_overview(ctx, tickers) -> dict[str, dict]` — `{bare: {call_volume, put_volume, call_open_interest, put_open_interest, iv_rank}}`.
  - `snapshot_fields(ctx, tickers) -> dict[str, dict]` — `{bare: {last_price, highest52weeks_price, volume_ratio, total_market_val}}`.

- [ ] **Step 1: Add the four functions**

Append to `src/adapters/moomoo/research.py` (it already imports `RET_OK`, `Market`, etc. from `moomoo` and defines `_us`/`_bare`; add `PeriodType` to that import):

```python
def _unwrap(out):
    """moomoo calls return (ret, data) — but some return longer tuples. Normalize."""
    if isinstance(out, tuple):
        return out[0], (out[1] if len(out) > 1 else None)
    return RET_OK, out


def capital_flow_daily(ctx, ticker):
    """Daily capital-flow records for one US name (newest ~1yr). [] on any failure."""
    from moomoo import PeriodType
    try:
        ret, data = _unwrap(ctx.get_capital_flow(_us(ticker), period_type=PeriodType.DAY))
        if ret != RET_OK or data is None or not len(data):
            return []
        return data.to_dict("records")
    except Exception:
        return []


def short_interest(ctx, ticker):
    """Short-interest readings for one US name. [] on any failure."""
    try:
        ret, data = _unwrap(ctx.get_short_interest(_us(ticker)))
        if ret != RET_OK or data is None or not len(data):
            return []
        return data.to_dict("records")
    except Exception:
        return []


def option_overview(ctx, tickers):
    """Batched option overview → {bare: {call/put volume+OI, iv_rank}}. {} on failure."""
    try:
        ret, data = _unwrap(ctx.get_option_underlying_overview([_us(t) for t in tickers]))
        if ret != RET_OK or data is None or not len(data):
            return {}
        keep = ["call_volume", "put_volume", "call_open_interest",
                "put_open_interest", "iv_rank"]
        out = {}
        for rec in data.to_dict("records"):
            out[_bare(rec.get("code", ""))] = {k: rec.get(k) for k in keep}
        return out
    except Exception:
        return {}


def snapshot_fields(ctx, tickers):
    """Batched snapshot → {bare: {last_price, highest52weeks_price, volume_ratio,
    total_market_val}}. {} on failure."""
    try:
        ret, data = _unwrap(ctx.get_market_snapshot([_us(t) for t in tickers]))
        if ret != RET_OK or data is None or not len(data):
            return {}
        keep = ["last_price", "highest52weeks_price", "volume_ratio", "total_market_val"]
        out = {}
        for rec in data.to_dict("records"):
            out[_bare(rec.get("code", ""))] = {k: rec.get(k) for k in keep}
        return out
    except Exception:
        return {}
```

- [ ] **Step 2: Verify it imports and compiles**

Run: `/usr/bin/python3 -c "import sys; sys.path.insert(0,'src'); from adapters.moomoo import research; print([f for f in dir(research) if not f.startswith('_')])"`
Expected: the list includes `capital_flow_daily`, `short_interest`, `option_overview`, `snapshot_fields`.

- [ ] **Step 3: Live dry-run (OpenD must be up)**

Run:
```bash
/usr/bin/python3 - <<'PY'
import sys; sys.path.insert(0, "src")
from adapters.moomoo.client import quote_ctx
from adapters.moomoo import research as mm
ctx = quote_ctx()
print("capflow rows:", len(mm.capital_flow_daily(ctx, "AAPL")))
print("short rows:", len(mm.short_interest(ctx, "AAPL")))
print("overview:", mm.option_overview(ctx, ["AAPL", "MSFT"]).keys())
print("snapshot:", mm.snapshot_fields(ctx, ["AAPL", "MSFT"]).get("AAPL"))
ctx.close()
PY
```
Expected: capflow rows ≈ 250, short rows ≈ 10, overview keys `dict_keys(['AAPL','MSFT'])`, snapshot AAPL dict with the four fields. (If OpenD is down every call returns empty — that is the non-fatal contract, not a bug; note it and retry when OpenD is up.)

- [ ] **Step 4: Commit**

```bash
git add src/adapters/moomoo/research.py
git commit -m "feat(moomoo): panel pull functions (capital flow, short, option overview, snapshot)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Collector orchestrator (`scripts/collect_signals.py`)

Reads the book, pulls (Task 2), distills (Task 1), assembles the `signal_panel` event, appends it, and phone-alerts on an OpenD gap. Non-fatal throughout. Runs under system python3.

**Files:**
- Create: `scripts/collect_signals.py`

**Interfaces:**
- Consumes: `research_store.read_current`, `research_store.store.append_journal`, `notify.push`, `signal_panel.distill_*` (Task 1), `adapters.moomoo` (Task 2).

- [ ] **Step 1: Write the collector**

Create `scripts/collect_signals.py`:

```python
"""Weekly moomoo signal-panel collector — forward-log decision context to the ledger.

Runs under SYSTEM /usr/bin/python3 (moomoo needs 3.10), after the Sunday rebalance.
Reads the held book, pulls four moomoo signals per name, distills them, and appends
ONE `signal_panel` journal event. Non-fatal: any failure -> null field + a `gaps`
entry; OpenD down / zero names -> phone push so a silent miss surfaces. Never trades.

    /usr/bin/python3 scripts/collect_signals.py [--dry]

Spec: docs/superpowers/specs/2026-07-23-moomoo-signal-panel-design.md
"""
import datetime as dt
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import signal_panel as sp                       # noqa: E402
from adapters.moomoo import research as mm       # noqa: E402
from adapters.moomoo.client import quote_ctx     # noqa: E402


def _held_book():
    """(list[(symbol, decision_id)], as_of) for names with target_weight>0."""
    import research_store as rs
    prod = rs.read_current()
    if prod is None:
        return [], None
    held = [(t.symbol, t.decision_id or f"{t.symbol}:{prod.as_of}")
            for t in prod.theses if (t.target_weight or 0) > 0]
    return held, prod.as_of


def _panel_for(ctx, sym, overview, snaps):
    """Assemble one name's panel dict; missing pieces -> null + a reason string."""
    gaps = []
    snap = snaps.get(sym, {})
    cf_rows = mm.capital_flow_daily(ctx, sym)
    if not cf_rows:
        gaps.append(f"{sym}: capital_flow")
    si_rows = mm.short_interest(ctx, sym)
    if not si_rows:
        gaps.append(f"{sym}: short_interest")
    if sym not in overview:
        gaps.append(f"{sym}: option_overview")
    if not snap:
        gaps.append(f"{sym}: snapshot")

    panel = {"capflow_bignet_20d": sp.distill_capflow(cf_rows, snap.get("total_market_val"))}
    panel.update(sp.distill_short(si_rows))
    panel.update(sp.distill_options(overview.get(sym, {})))
    panel.update(sp.distill_snapshot(snap))
    return panel, gaps


def main():
    dry = "--dry" in sys.argv
    held, as_of = _held_book()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {"event": "signal_panel", "as_of": as_of, "at": now,
             "source": "moomoo", "names": {}, "opend_ok": True, "gaps": []}

    if not held:
        print("no held book — nothing to collect")
        return

    syms = [s for s, _ in held]
    ctx = None
    try:
        ctx = quote_ctx()
        overview = mm.option_overview(ctx, syms)
        snaps = mm.snapshot_fields(ctx, syms)
        # OpenD down / systemic failure = both batched pulls empty for a real book
        if not overview and not snaps:
            event["opend_ok"] = False
            event["gaps"].append("OpenD unreachable or returned nothing")
        else:
            for sym, did in held:
                panel, gaps = _panel_for(ctx, sym, overview, snaps)
                panel["decision_id"] = did
                event["names"][sym] = panel
                event["gaps"].extend(gaps)
    except Exception as e:  # never crash the cycle
        event["opend_ok"] = False
        event["gaps"].append(f"collector error: {type(e).__name__}: {e}")
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    if dry:
        print(json.dumps(event, indent=2, default=str))
        return

    from research_store.store import append_journal
    append_journal(event)
    print(f"signal_panel: {len(event['names'])} names, {len(event['gaps'])} gaps, "
          f"opend_ok={event['opend_ok']}")

    if not event["opend_ok"] or not event["names"]:
        try:
            import notify
            notify.push("Agentic: signal panel gap",
                        f"opend_ok={event['opend_ok']} names={len(event['names'])} "
                        f"gaps={len(event['gaps'])}", tags="floppy_disk")
        except Exception:
            pass


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Compile + import check**

Run: `/usr/bin/python3 -m py_compile scripts/collect_signals.py && echo compile-ok`
Expected: `compile-ok`

- [ ] **Step 3: Live dry-run (no write)**

Run: `/usr/bin/python3 scripts/collect_signals.py --dry`
Expected: a JSON `signal_panel` event printed — `names` populated for the current held book (each with `decision_id` + the 9 scalars), `opend_ok: true`, `gaps` small/empty. (Empty book → "no held book"; OpenD down → `opend_ok:false` + a gap, which is the correct non-fatal behavior.)

- [ ] **Step 4: Commit**

```bash
git add scripts/collect_signals.py
git commit -m "feat(signals): weekly moomoo signal-panel collector -> ledger (non-fatal, alerting)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Cron wiring + docs note

Schedule the collector after the Sunday rebalance, under system python3, and record the new event type in the docs.

**Files:**
- Modify: `deploy/crontab.template`
- Modify: `docs/DATA_SOURCES.md`

- [ ] **Step 1: Add the cron line**

In `deploy/crontab.template`, after the SLOW LOOP block (the `0 20 * * 0 … run_slow_loop.sh` line), add:

```bash
# --- WEEKLY moomoo SIGNAL PANEL (system python3 — moomoo needs 3.10, not .venv) ---
# Sun 20:15, after the 20:00 rebalance writes the new book. Forward-logs a
# signal_panel journal event per held name for later meta-labeling. Non-fatal.
15 20 * * 0  cd /opt/agentic-trader && /usr/bin/python3 scripts/collect_signals.py >> /opt/agentic-trader/logs/signals.log 2>&1
```

- [ ] **Step 2: Verify crontab still parses**

Run: `grep -c collect_signals deploy/crontab.template`
Expected: `1`

- [ ] **Step 3: Document the event in DATA_SOURCES.md**

In `docs/DATA_SOURCES.md`, replace the closing "The signal-panel plan (in design)" section's last paragraph with a note that it is now BUILT: the `signal_panel` journal event (weekly, book-scoped, fields `capflow_bignet_20d`, `short_pct`, `days_to_cover`, `short_pct_chg`, `pc_vol_ratio`, `pc_oi_ratio`, `iv_rank`, `pct_52w_high`, `volume_ratio`), written by `scripts/collect_signals.py`.

- [ ] **Step 4: Commit**

```bash
git add deploy/crontab.template docs/DATA_SOURCES.md
git commit -m "feat(signals): schedule weekly panel collector + document signal_panel event

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **You are on the box; OpenD is up** — the live dry-runs (Task 2 Step 3, Task 3 Step 3) are the real verification for the I/O the `--selftest` can't cover. Run them.
- **Do not wire `collect_signals` into `run_slow_loop.sh` or the `.venv`** — it's a *separate* cron step under `/usr/bin/python3` (importing moomoo into `.venv` fails).
- **Field names are pinned to the 2026-07-23 verified schema** (`docs/DATA_SOURCES.md` §5b). If a moomoo column name has changed, fix the adapter's `keep`/`.get()` keys, not the distiller math.
- Verified before writing: `research_store`, `research_store.store.append_journal`, `notify`, and `pandas` all import under `/usr/bin/python3` (3.10), and `moomoo.PeriodType.DAY` exists.
