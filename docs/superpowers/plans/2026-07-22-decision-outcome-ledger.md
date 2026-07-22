# Decision → Outcome Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `research_store/journal.jsonl` into a complete, self-verifying, backed-up Decision→Outcome ledger that is both a real-money audit trail and a learning corpus.

**Architecture:** Extend the existing append-only journal (no new store backend). Add a pure helper module (`src/ledger.py`) for outcome math + derived views; a deterministic reconcile-verify-alarm script that keeps the journal complete against Robinhood ground truth; wire the already-existing-but-unwired `record_outcome()` into the exit paths; thread a `decision_id` join key through; and mirror the non-regenerable files to a private off-box git repo after each run.

**Tech Stack:** Python 3 (run via `.venv/bin/python` — system python is too old), stdlib only (json, datetime, pathlib), bash for deploy, `src/notify.py` for phone alerts. No pytest — the repo convention is an embedded `--selftest` entrypoint per module, run with `.venv/bin/python <module> --selftest`.

## Global Constraints

- **No new live-trading surface.** Nothing in this plan places, modifies, or cancels an order. Reconcile only READS Robinhood's order dump and APPENDS to the journal.
- **Robinhood is agent-only.** Deterministic Python cannot call RH MCP. RH data enters via agent-written files (`research_store/rh/*.json`), same pattern as today.
- **Run Python with `.venv/bin/python`** — never system `python`.
- **Append via `store.append_journal()`** (atomic, one JSON object per line). Never hand-edit `journal.jsonl`.
- **Idempotent writes.** `order_id` is the dedup key for executions; `decision_id = "<SYMBOL>:<as_of>"` is the join key. Re-running any step must never double-count.
- **Alerts via `src/notify.py` → `push(title, message, tags="...")`** — never raises.
- **Backups are non-fatal** — a failed backup alerts the phone but never fails or blocks a trading run.
- **Never set `ANTHROPIC_API_KEY`** in any deploy script (billing footgun; existing scripts guard on it).
- **Testing convention:** each new module gets a `_selftest()` run via `if __name__ == "__main__"` on `--selftest`, printing `selftest OK: ...` on success — matching `scripts/fast_loop.py`, `src/ti_signals.py`, etc.

---

### Task 1: `src/ledger.py` — join key + outcome math (pure)

**Files:**
- Create: `src/ledger.py`

**Interfaces:**
- Produces:
  - `decision_id(symbol: str, as_of: str) -> str`
  - `outcome_from_exit(*, symbol: str, as_of: str, entry_price: float, exit_price: float, stop: float | None, targets: list, exit_reason: str, entry_date: str, exit_date: str, spy_entry: float | None = None, spy_exit: float | None = None) -> dict`

- [ ] **Step 1: Write the failing selftest**

Create `src/ledger.py` with ONLY the selftest body first (functions not yet defined):

```python
"""Decision→Outcome Ledger — pure helpers over research_store/journal.jsonl.

No RH I/O, no network, no clock reads inside the pure functions (dates are
passed in). Builds the outcome LABEL and derived views that make the journal a
complete, learnable record.

Spec: docs/superpowers/specs/2026-07-22-decision-outcome-ledger-design.md
"""
from __future__ import annotations

import datetime as _dt


def _selftest() -> None:
    # decision_id: upper-cases symbol, joins on as_of
    assert decision_id("xle", "2026-07-20") == "XLE:2026-07-20"

    # a winning target exit vs SPY
    o = outcome_from_exit(
        symbol="XLE", as_of="2026-07-06", entry_price=100.0, exit_price=110.0,
        stop=95.0, targets=[108.0, 115.0], exit_reason="target",
        entry_date="2026-07-06", exit_date="2026-07-20",
        spy_entry=500.0, spy_exit=505.0,
    )
    assert o["decision_id"] == "XLE:2026-07-06"
    assert o["pnl_pct"] == 0.1           # (110-100)/100
    assert o["holding_days"] == 14
    assert o["hit_target"] is True       # 110 >= first target 108
    assert o["hit_stop"] is False
    assert o["return_vs_spy"] == 0.09     # 0.10 - 0.01
    assert o["exit_reason"] == "target"

    # a stop-out, no SPY context supplied
    s = outcome_from_exit(
        symbol="MU", as_of="2026-07-10", entry_price=200.0, exit_price=190.0,
        stop=192.0, targets=[220.0], exit_reason="stopped",
        entry_date="2026-07-10", exit_date="2026-07-13",
    )
    assert s["pnl_pct"] == -0.05
    assert s["hit_stop"] is True         # 190 <= stop 192
    assert s["hit_target"] is False
    assert s["return_vs_spy"] is None    # no spy inputs -> None
    assert s["holding_days"] == 3

    print("selftest OK: decision_id, outcome_from_exit (win/target, loss/stop, no-spy)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python src/ledger.py --selftest`
Expected: `NameError: name 'decision_id' is not defined`

- [ ] **Step 3: Implement the two functions**

Insert above `_selftest`:

```python
def decision_id(symbol: str, as_of: str) -> str:
    """Stable join key for one position across its lifecycle."""
    return f"{symbol.upper()}:{as_of}"


def _days_between(start: str, end: str) -> int:
    """Whole calendar days from start to end (ISO YYYY-MM-DD)."""
    a = _dt.date.fromisoformat(start[:10])
    b = _dt.date.fromisoformat(end[:10])
    return (b - a).days


def outcome_from_exit(*, symbol, as_of, entry_price, exit_price, stop, targets,
                      exit_reason, entry_date, exit_date,
                      spy_entry=None, spy_exit=None) -> dict:
    """Compute the outcome LABEL for a fully-closed position — pure arithmetic.

    entry_price = the position's average cost; exit_price = the realized sell
    price. return_vs_spy is None unless BOTH spy_entry and spy_exit are given.
    """
    entry_price = float(entry_price)
    exit_price = float(exit_price)
    pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0.0

    hit_stop = stop is not None and exit_price <= float(stop)
    hit_target = bool(targets) and exit_price >= float(targets[0])

    rel = None
    if spy_entry and spy_exit:
        spy_ret = (float(spy_exit) - float(spy_entry)) / float(spy_entry)
        rel = round(pnl_pct - spy_ret, 4)

    return {
        "decision_id": decision_id(symbol, as_of),
        "status": exit_reason,
        "exit_reason": exit_reason,
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "pnl_pct": round(pnl_pct, 4),
        "holding_days": _days_between(entry_date, exit_date),
        "hit_stop": hit_stop,
        "hit_target": hit_target,
        "return_vs_spy": rel,
    }
```

- [ ] **Step 4: Run the selftest to verify it passes**

Run: `.venv/bin/python src/ledger.py --selftest`
Expected: `selftest OK: decision_id, outcome_from_exit (win/target, loss/stop, no-spy)`

- [ ] **Step 5: Commit**

```bash
git add src/ledger.py
git commit -m "feat(ledger): pure outcome math + decision_id join key"
```

---

### Task 2: `src/ledger.py` — derived views (#5, #7)

**Files:**
- Modify: `src/ledger.py`

**Interfaces:**
- Consumes: journal entries (list of dicts) as produced by `store.read_journal()`.
- Produces:
  - `realized_history(journal: list[dict]) -> list[dict]`
  - `position_history(journal: list[dict]) -> dict`  (`{symbol: [event, ...]}`)

- [ ] **Step 1: Extend the selftest with the failing cases**

In `src/ledger.py`, append to `_selftest()` (before its `print`), then update the print string:

```python
    # --- derived views over a fixture journal ---
    jrnl = [
        {"event": "product", "as_of": "2026-07-06"},
        {"event": "execution", "ts": "2026-07-06T14:00:00+00:00", "fills": [
            {"symbol": "XLE", "side": "buy", "amount": 5.0, "order_id": "o1", "avg_price": 100.0}]},
        {"event": "execution", "ts": "2026-07-20T14:00:00+00:00", "fills": [
            {"symbol": "XLE", "side": "sell", "amount": 5.5, "order_id": "o2", "avg_price": 110.0}]},
        {"event": "outcome", "symbol": "XLE", "at": "2026-07-20T14:01:00+00:00",
         "outcome": {"pnl_pct": 0.1, "status": "target", "decision_id": "XLE:2026-07-06"}},
    ]
    rh = realized_history(jrnl)
    assert rh == [{"symbol": "XLE", "at": "2026-07-20T14:01:00+00:00",
                   "pnl_pct": 0.1, "status": "target",
                   "decision_id": "XLE:2026-07-06"}], rh

    ph = position_history(jrnl)
    assert set(ph) == {"XLE"}
    assert [(e["side"], e["order_id"]) for e in ph["XLE"]] == [("buy", "o1"), ("sell", "o2")]
```

Change the final print to:

```python
    print("selftest OK: decision_id, outcome_from_exit, realized_history, position_history")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/ledger.py --selftest`
Expected: `NameError: name 'realized_history' is not defined`

- [ ] **Step 3: Implement the two views**

Insert before `_selftest`:

```python
def realized_history(journal: list) -> list:
    """Full-life realized-P&L series from `outcome` events (not RH's rolling
    window). Each item: {symbol, at, pnl_pct, status, decision_id}."""
    out = []
    for e in journal:
        if e.get("event") != "outcome":
            continue
        o = e.get("outcome") or {}
        out.append({
            "symbol": e.get("symbol"),
            "at": e.get("at"),
            "pnl_pct": o.get("pnl_pct"),
            "status": o.get("status"),
            "decision_id": o.get("decision_id"),
        })
    return out


def position_history(journal: list) -> dict:
    """Reconstruct per-symbol execution history from `execution` events —
    the ledger-derived record of buys/sells over time (dollar-notional +
    order_id). Skipped orders (no fill) are excluded."""
    hist: dict = {}
    for e in journal:
        if e.get("event") != "execution":
            continue
        for f in e.get("fills") or []:
            if f.get("status") == "skipped":
                continue
            sym = f.get("symbol")
            if not sym:
                continue
            hist.setdefault(sym, []).append({
                "ts": e.get("ts"),
                "side": f.get("side"),
                "amount": f.get("amount"),
                "avg_price": f.get("avg_price"),
                "order_id": f.get("order_id"),
            })
    return hist
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/ledger.py --selftest`
Expected: `selftest OK: decision_id, outcome_from_exit, realized_history, position_history`

- [ ] **Step 5: Commit**

```bash
git add src/ledger.py
git commit -m "feat(ledger): derived realized-P&L + position history views (#5,#7)"
```

---

### Task 3: `scripts/reconcile_ledger.py` — reconcile · verify · alarm (#3)

**Files:**
- Create: `scripts/reconcile_ledger.py`

**Interfaces:**
- Consumes: `store.read_journal()`, `store.append_journal()`, `notify.push()`.
- Produces (pure, testable core):
  - `journaled_order_ids(journal: list[dict]) -> set[str]`
  - `missing_orders(journaled: set[str], rh_orders: list[dict]) -> list[dict]`  (filled RH orders not journaled)
  - `heal_event(missing: list[dict], ts: str) -> dict | None`  (an `execution` event with `source="reconcile"`, or None if nothing missing)

- [ ] **Step 1: Write the failing selftest**

Create `scripts/reconcile_ledger.py` with the selftest and stubs-absent:

```python
"""Reconcile the journal against Robinhood ground truth — auto-heal, then alarm.

Robinhood is the source of truth for what executed. Only the AGENT can reach RH,
so it writes an order dump to research_store/rh/orders_dump.json; this script is
the deterministic half: any FILLED order missing from journal.jsonl is appended
(source="reconcile"), then we re-verify and — if any RH fill is still unjournaled
— phone-alarm and exit non-zero. A silently-incomplete ledger becomes impossible.

Idempotent: keyed on order_id, so re-running never double-appends.

Spec: docs/superpowers/specs/2026-07-22-decision-outcome-ledger-design.md
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from research_store import store  # noqa: E402
from notify import push           # noqa: E402

ORDERS_DUMP = REPO / "research_store" / "rh" / "orders_dump.json"


def _selftest() -> None:
    jrnl = [
        {"event": "execution", "fills": [
            {"symbol": "XLE", "side": "buy", "order_id": "o1", "avg_price": 100.0}]},
        {"event": "product", "as_of": "2026-07-06"},
    ]
    assert journaled_order_ids(jrnl) == {"o1"}

    rh = [
        {"order_id": "o1", "symbol": "XLE", "side": "buy", "state": "filled",
         "quantity": 0.05, "average_price": 100.0},
        {"order_id": "o2", "symbol": "MU", "side": "sell", "state": "filled",
         "quantity": 0.01, "average_price": 190.0},
        {"order_id": "o3", "symbol": "AAPL", "side": "buy", "state": "cancelled",
         "quantity": 0.0, "average_price": None},
    ]
    miss = missing_orders({"o1"}, rh)
    assert [m["order_id"] for m in miss] == ["o2"], miss   # o1 known, o3 not filled

    ev = heal_event(miss, ts="2026-07-20T15:00:00+00:00")
    assert ev["event"] == "execution" and ev["source"] == "reconcile"
    assert [f["order_id"] for f in ev["fills"]] == ["o2"]
    assert ev["fills"][0]["side"] == "sell" and ev["fills"][0]["avg_price"] == 190.0

    # nothing missing -> no event
    assert heal_event([], ts="2026-07-20T15:00:00+00:00") is None
    # re-run idempotency: once o2 is journaled, it is no longer missing
    assert missing_orders({"o1", "o2"}, rh) == []

    print("selftest OK: journaled_order_ids, missing_orders, heal_event (idempotent)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python scripts/reconcile_ledger.py --selftest`
Expected: `NameError: name 'journaled_order_ids' is not defined`

- [ ] **Step 3: Implement the pure core + the `main()` driver**

Insert above `_selftest`:

```python
def journaled_order_ids(journal: list) -> set:
    """Every order_id already recorded in an execution event."""
    ids = set()
    for e in journal:
        if e.get("event") != "execution":
            continue
        for f in e.get("fills") or []:
            oid = f.get("order_id")
            if oid:
                ids.add(oid)
    return ids


def missing_orders(journaled: set, rh_orders: list) -> list:
    """FILLED RH orders whose order_id is not yet in the journal."""
    out = []
    for o in rh_orders:
        if o.get("state") != "filled":
            continue
        oid = o.get("order_id")
        if oid and oid not in journaled:
            out.append(o)
    return out


def heal_event(missing: list, ts: str) -> dict | None:
    """Build one execution event (source='reconcile') for the missing orders,
    or None if nothing is missing."""
    if not missing:
        return None
    fills = [{
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "order_id": o.get("order_id"),
        "avg_price": o.get("average_price"),
        "quantity": o.get("quantity"),
        "status": "filled",
    } for o in missing]
    return {"event": "execution", "source": "reconcile", "ts": ts,
            "n": len(fills), "fills": fills}


def main() -> None:
    if not ORDERS_DUMP.exists():
        # No dump written this run — nothing to reconcile against. Not an error.
        print(f"no orders dump at {ORDERS_DUMP} — skipping reconcile")
        return
    rh_orders = json.loads(ORDERS_DUMP.read_text())
    journal = store.read_journal()
    journaled = journaled_order_ids(journal)

    missing = missing_orders(journaled, rh_orders)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ev = heal_event(missing, ts)
    if ev:
        store.append_journal(ev)
        print(f"reconcile: healed {ev['n']} unjournaled fill(s): "
              + ", ".join(f["order_id"] for f in ev["fills"]))

    # Verify: after healing, NO filled RH order may be absent from the journal.
    journal = store.read_journal()
    still = missing_orders(journaled_order_ids(journal), rh_orders)
    if still:
        push("Agentic: LEDGER DIVERGENCE",
             f"{len(still)} filled RH order(s) not in journal after reconcile: "
             + ", ".join(o.get("order_id", "?") for o in still),
             tags="rotating_light")
        sys.exit(f"ledger divergence: {len(still)} unjournaled filled orders")
    print("reconcile: journal complete vs RH dump")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python scripts/reconcile_ledger.py --selftest`
Expected: `selftest OK: journaled_order_ids, missing_orders, heal_event (idempotent)`

- [ ] **Step 5: Commit**

```bash
git add scripts/reconcile_ledger.py
git commit -m "feat(reconcile): auto-heal journal vs RH order dump, then verify+alarm (#3)"
```

---

### Task 4: Thread `decision_id` through context + outcome

**Files:**
- Modify: `src/research_store/models.py:38` (add field)
- Modify: `src/research_store/__init__.py:98-111` (`record_outcome` stamps decision_id)
- Modify: `src/research_store/__init__.py` `write_product` (stamp decision_id onto theses)

**Interfaces:**
- Consumes: `ledger.decision_id` (Task 1).
- Produces: every held thesis in `current.json` and every `outcome` event carries `decision_id`.

- [ ] **Step 1: Add the field to the Thesis dataclass**

In `src/research_store/models.py`, after line 38 (`outcome: dict | None = None`):

```python
    decision_id: str | None = None                    # "<SYMBOL>:<as_of>" join key
```

- [ ] **Step 2: Stamp decision_id when a product is written**

In `src/research_store/__init__.py`, at the top add the import (near the other imports):

```python
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from ledger import decision_id as _decision_id  # noqa: E402
```

In `write_product`, immediately before `store.save_current(product.to_dict(), archive=archive)`:

```python
    for t in product.theses:
        if t.as_of:
            t.decision_id = _decision_id(t.symbol, t.as_of)
```

- [ ] **Step 3: Stamp decision_id into the outcome event**

In `record_outcome`, change the journal append (currently at `__init__.py:110`) to include the key when the matched thesis has an `as_of`. Replace the loop+append with:

```python
    did = None
    for t in d.get("theses", []):
        if str(t.get("symbol", "")).upper() == symbol.upper():
            t["outcome"] = outcome
            did = t.get("decision_id") or _decision_id(symbol, t.get("as_of", ""))
            break
    else:
        raise KeyError(f"{symbol} not in current product")
    store.save_current(d, archive=False)
    ev = {"event": "outcome", "symbol": symbol.upper(), "at": now_iso, "outcome": outcome}
    if did:
        ev["decision_id"] = did
    store.append_journal(ev)
```

- [ ] **Step 4: Verify the existing store selftest/import still works**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from research_store import read_current, write_product; from research_store.models import Thesis; print('import OK', 'decision_id' in [f.name for f in __import__('dataclasses').fields(Thesis)])"`
Expected: `import OK True`

- [ ] **Step 5: Commit**

```bash
git add src/research_store/models.py src/research_store/__init__.py
git commit -m "feat(ledger): thread decision_id through theses + outcome events"
```

---

### Task 5: Wire auto-outcome, rationale, and reconcile into the agent prompts

**Files:**
- Modify: `prompts/fast_loop.md` (steps 8–9 region)
- Modify: `prompts/exit.md` (steps 5–7 region)

No code test — the deliverable is prompt text plus a validation that the scripts it references run. Follow the exact wording below (the prompts are executable procedure for the headless agent).

- [ ] **Step 1: Add an orders dump + reconcile step to `prompts/fast_loop.md`**

In `prompts/fast_loop.md`, in step 8 (Reconcile), after the `get_equity_orders(order_id=...)` sentence, add:

```
   Also write ALL orders you touched this run (placed AND filled-from-prior) to
   `research_store/rh/orders_dump.json` — a JSON array of
   `{order_id, symbol, side, quantity, average_price, state, executed_at}` from
   `get_equity_orders` (state is RH's, e.g. "filled"/"cancelled"). This is the
   ground-truth dump the reconciler checks the journal against.
```

At the END of step 9 (after the `record_fills.py` sentence), add:

```
   Then run `.venv/bin/python scripts/reconcile_ledger.py`. It appends any RH
   fill missing from the journal and PHONE-ALARMS + exits non-zero if the
   journal is still incomplete. Do not suppress its exit code.
```

- [ ] **Step 2: Add a short rationale to fills in `prompts/fast_loop.md` step 9**

In `prompts/fast_loop.md` step 9, change the fills-schema sentence to add an optional `note`:

Find: `{symbol, side, amount, order_id, status, avg_price}`
Replace with: `{symbol, side, amount, order_id, status, avg_price, note}` where
`note` is ≤15 words on WHY (e.g. `"open: momentum rank 1"`, `"rebalance trim"`,
`"stop breach"`) — the joinable rationale, not prose.

- [ ] **Step 3: Wire auto-outcome + reconcile into `prompts/exit.md`**

In `prompts/exit.md`, replace step 7's final paragraph (the realized-P&L refresh) end with an added outcome + reconcile step. After the `realized.json` write instructions, add a new step:

```
7c. **Record the outcome (the learning label).** For each symbol you sold to a
    FULL close (position now zero), compute and journal its outcome. In a short
    python snippet run with `.venv/bin/python`:
      - entry_price = that symbol's `avg_cost` from the pre-sell
        `research_store/rh/positions.json`
      - exit_price  = the sell's `average_price` from `get_equity_orders`
      - stop/targets/as_of = from that symbol's thesis in
        `research_store/current.json`
      - spy_entry/spy_exit = optional; pass None if unknown
    Call:
      `from ledger import outcome_from_exit` (add `src` to sys.path) to build the
      dict, then `from research_store import record_outcome` and
      `record_outcome(symbol, outcome, now_iso)`. This attaches the label to the
      thesis and appends an `outcome` event. Partial (scale-out) exits: skip —
      only full closes get an outcome (known first-cut limitation).

7d. **Reconcile.** Write `research_store/rh/orders_dump.json` (same schema as the
    fast loop's step 8) from `get_equity_orders`, then run
    `.venv/bin/python scripts/reconcile_ledger.py`. Don't suppress its exit code.
```

- [ ] **Step 4: Validate the referenced scripts run**

Run: `.venv/bin/python scripts/reconcile_ledger.py --selftest && .venv/bin/python src/ledger.py --selftest`
Expected: both print `selftest OK: ...`

- [ ] **Step 5: Commit**

```bash
git add prompts/fast_loop.md prompts/exit.md
git commit -m "feat(prompts): wire auto-outcome, rationale note, and reconcile into fast/exit loops"
```

---

### Task 6: `deploy/backup_ledger.sh` — private off-box mirror (Component E)

**Files:**
- Create: `deploy/backup_ledger.sh`
- Modify: `deploy/run_fast_loop.sh`, `deploy/run_slow_loop.sh` (call it, best-effort)
- Modify: `deploy/crontab.template` (nightly catch-all)

**Interfaces:**
- Backs up ONLY the non-regenerable files: `research_store/journal.jsonl`, `research_store/history/equity.jsonl`, `research_store/flows.jsonl`, and `research_store/archive/`.
- Destination: private repo `github.com/aye5788/agentic-trader-ledger`, cloned once to `$HOME/agentic-trader-ledger`.

- [ ] **Step 1: Write the script**

Create `deploy/backup_ledger.sh`:

```bash
#!/usr/bin/env bash
# Off-box backup of the NON-REGENERABLE ledger data to a private git mirror.
# The trade record + outcome labels cannot be rebuilt from code (unlike prices
# and current.json), and research_store/ is git-ignored with no other backup.
# Non-fatal by contract: a failure phone-alerts but never blocks a trading run.
#
# One-time setup on the box:
#   git clone git@github.com:aye5788/agentic-trader-ledger.git "$HOME/agentic-trader-ledger"
#   (or https with a stored credential/token; verify `git -C ... push` works)
set -uo pipefail   # NOT -e: this script must never hard-fail its caller
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
MIRROR="${LEDGER_MIRROR:-$HOME/agentic-trader-ledger}"

fail() { .venv/bin/python -c "import sys; sys.path.insert(0,'src'); from notify import push; push('Agentic: ledger backup FAILED', '''$1''', tags='floppy_disk')" 2>/dev/null || true; echo "backup_ledger: $1" >&2; exit 0; }

[ -d "$MIRROR/.git" ] || fail "mirror not cloned at $MIRROR — run the one-time git clone (see script header)"

# Copy only the non-regenerable files.
mkdir -p "$MIRROR/history" "$MIRROR/archive"
cp -f research_store/journal.jsonl        "$MIRROR/journal.jsonl"        2>/dev/null || true
cp -f research_store/history/equity.jsonl "$MIRROR/history/equity.jsonl" 2>/dev/null || true
cp -f research_store/flows.jsonl          "$MIRROR/flows.jsonl"          2>/dev/null || true
cp -f research_store/archive/*.json       "$MIRROR/archive/"             2>/dev/null || true

cd "$MIRROR" || fail "cannot cd $MIRROR"
git add -A
if git diff --cached --quiet; then
  echo "backup_ledger: no changes"; exit 0
fi
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
git commit -q -m "ledger backup $STAMP" || fail "git commit failed"
git push -q || fail "git push failed (check credentials/network)"
echo "backup_ledger: pushed $STAMP"
```

- [ ] **Step 2: Make it executable and run its no-op path**

Run: `chmod +x deploy/backup_ledger.sh && LEDGER_MIRROR=/tmp/nomirror deploy/backup_ledger.sh; echo "exit=$?"`
Expected: prints a `backup_ledger: mirror not cloned ...` line and `exit=0` (never hard-fails the caller).

- [ ] **Step 3: Wire into the deploy scripts (best-effort, after the run)**

In `deploy/run_fast_loop.sh`, after the final `log_equity.py` line, add:

```bash
# mirror the non-regenerable ledger off-box (best-effort; never fails the run)
deploy/backup_ledger.sh || true
```

In `deploy/run_slow_loop.sh`, add the same line at the very end.

- [ ] **Step 4: Add a nightly catch-all to the crontab**

In `deploy/crontab.template`, add (pick an idle hour, after the fast loop):

```
# Nightly off-box ledger backup (catch-all in case a run's inline backup missed)
30 22 * * *  cd /opt/agentic-trader && deploy/backup_ledger.sh >> logs/backup.log 2>&1
```

- [ ] **Step 5: Commit**

```bash
git add deploy/backup_ledger.sh deploy/run_fast_loop.sh deploy/run_slow_loop.sh deploy/crontab.template
git commit -m "feat(deploy): off-box private-git backup of the non-regenerable ledger (Component E)"
```

---

### Task 7 (optional, last): adopt derived views in the dashboard / letter_facts

**Files:**
- Modify: `scripts/letter_facts.py` or `dashboard/app.py` (incremental, only if desired)

This is optional polish per the spec (§4.5) — the ledger is fully functional without it. When adopted, replace reads of `rh/realized.json`'s rolling window with `ledger.realized_history(store.read_journal())` for full-life P&L. Defer unless explicitly requested; no task steps mandated.

---

## Self-Review

**Spec coverage:**
- #3 silent hole → Task 3 (reconcile/verify/alarm) + Task 5 (wired into both loops). ✓
- #6 outcomes null → Task 1 (math) + Task 4 (decision_id on outcome) + Task 5 step 3 (wired `record_outcome`). ✓
- #1 rationale → Task 5 step 2 (`note` field) + existing reentry decisions. ✓
- #5 P&L ages out → Task 2 (`realized_history`). ✓
- #7 position history → Task 2 (`position_history`). ✓
- Component E durability → Task 6. ✓
- decision_id join key → Task 1 + Task 4. ✓
- Deferred (#2 intraday, transcript prose) → correctly absent. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; Task 7 is explicitly optional, not a placeholder. ✓

**Type consistency:** `decision_id(symbol, as_of)` signature identical in Tasks 1/4. `outcome_from_exit` keyword names match between Task 1 selftest, implementation, and Task 5 prompt wiring. `heal_event`/`missing_orders`/`journaled_order_ids` names consistent across Task 3 selftest, implementation, and `main()`. Journal `execution` event shape (`fills[]` with `order_id`) consistent across Tasks 2, 3, and existing `record_fills.py`. ✓

**Known first-cut limitations (documented, intentional):** partial/scale-out exits get no outcome (only full closes); `return_vs_spy` is None unless SPY entry/exit are supplied; `position_history` records dollar-notional events, not reconstructed share counts.
